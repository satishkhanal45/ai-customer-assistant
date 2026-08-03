import asyncio
import httpx
from .config import CrawlConfig, CrawlMode
from .models import CrawlDocument
from .utils import normalize_url
from .parser import extract_links, classify_links
from .fetcher import fetch_page
from .extractor import extract_markdown, extract_meta
from .exception import CrawlerError
from .queues import next_frontier

async def _process_url(url: str, depth: int, config: CrawlConfig, client: httpx.AsyncClient) -> CrawlDocument:
    try:
        page = await fetch_page(url, client, config)
        markdown = extract_markdown(page.html, page.url)
        meta = extract_meta(page.html, page.url)
        links = extract_links(page.html, page.url)
        internal, external = classify_links(links, config.allowed_domains)
        return CrawlDocument(
            url=page.url, title=meta.title, markdown=markdown, html=page.html,
            depth=depth, status_code=page.status_code,
            internal_links=internal, external_links=external,
        )
    except CrawlerError as e:
        return CrawlDocument(
            url=url, title="", markdown="", html="", depth=depth,
            status_code=0, error=str(e),
        )

async def _crawl_level(
    frontier: frozenset[str], depth: int, config: CrawlConfig,
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore,
) -> tuple[CrawlDocument, ...]:
    async def bounded(url: str) -> CrawlDocument:
        async with semaphore:
            return await _process_url(url, depth, config, client)
    return tuple(await asyncio.gather(*map(bounded, frontier)))

async def _crawl_site(root: str, config: CrawlConfig, client: httpx.AsyncClient) -> tuple[CrawlDocument, ...]:
    semaphore = asyncio.Semaphore(config.concurrent_requests)

    async def go(frontier: frozenset[str], depth: int, visited: frozenset[str], acc: tuple[CrawlDocument, ...]):
        if not frontier or len(visited) >= config.max_pages or depth > config.max_depth:
            return acc
        docs = await _crawl_level(frontier, depth, config, client, semaphore)
        visited_now = visited | frontier
        frontier_next = next_frontier(
            tuple(d.internal_links for d in docs if d.error is None), visited_now, config.max_pages
        )
        return await go(frontier_next, depth + 1, visited_now, acc + docs)

    return await go(frozenset({normalize_url(root)}), 0, frozenset(), ())

async def _crawl_page(root: str, config: CrawlConfig, client: httpx.AsyncClient) -> tuple[CrawlDocument, ...]:
    return (await _process_url(normalize_url(root), 0, config, client),)

MODE_HANDLERS = {
    CrawlMode.PAGE: _crawl_page,
    CrawlMode.SITE: _crawl_site,
}

class Crawler:
    def __init__(self, config: CrawlConfig):
        self._config = config

    async def crawl(self, url: str, client: httpx.AsyncClient | None = None) -> tuple[CrawlDocument, ...]:
        owns_client = client is None
        active = client or httpx.AsyncClient()
        try:
            handler = MODE_HANDLERS[self._config.mode]
            return await handler(url, self._config, active)
        finally:
            if owns_client:
                await active.aclose()
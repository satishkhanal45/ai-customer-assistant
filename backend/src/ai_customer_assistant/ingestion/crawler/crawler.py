"""Crawler v2 orchestration: two-phase discover -> crawl_confirmed.

``discover``           sitemap-first (decision 3), falling back to a link-only
                       BFS (decision 4) using the existing ``queue.py``
                       frontier logic. No markdown extraction during discovery.
``crawl_confirmed``    sequential execution (decision 2) over the exact list
                       passed in — no re-discovery, no partial selection. One
                       browser instance is opened for the whole call (decision
                       9); a fresh ``Page``/context is created per URL by the
                       fetcher and closed immediately after use. A fixed delay
                       runs between items (decision 8).
"""

import asyncio
from typing import Optional

from . import documents
from .config import CrawlConfig
from .exception import CrawlerError
from .fetcher import fetch_bytes, fetch_page
from .models import CrawlDocument, DiscoveredPage, DiscoveryResult
from .parser import classify_links, extract_links
from .utils import normalize_url
from urllib.parse import urlsplit


async def _open_browser(config: CrawlConfig):
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(user_agent=config.user_agent)
    return context, browser, playwright


async def _close_browser(context, browser, playwright) -> None:
    await context.close()
    await browser.close()
    await playwright.stop()


def _classify(urls: tuple[str, ...]) -> tuple[DiscoveredPage, ...]:
    return tuple(documents.classify_url(url) for url in urls)


def _extract_markdown(html: str, url: str) -> str:
    from .extractor import extract_markdown

    return extract_markdown(html, url)


def _extract_title(html: str, url: str) -> str:
    from .extractor import extract_meta

    return extract_meta(html, url).title


async def _fetch_sitemap_url(
    root: str, config: CrawlConfig, context
) -> Optional[str]:
    """Fetch robots.txt and return the first ``Sitemap:`` URL if present."""
    parts = urlsplit(root)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        robots = await fetch_bytes(robots_url, context, config)
    except CrawlerError:
        return None
    if robots.status_code >= 400:
        return None
    from .sitemap import parse_robots_sitemap

    return parse_robots_sitemap(robots.data.decode("utf-8", errors="replace"))


async def _discover_by_sitemap(
    root: str, config: CrawlConfig, context
) -> Optional[tuple[DiscoveredPage, ...]]:
    sitemap_url = await _fetch_sitemap_url(root, config, context)
    if sitemap_url is None:
        return None
    try:
        sitemap = await fetch_bytes(sitemap_url, context, config)
    except CrawlerError:
        return None
    if sitemap.status_code >= 400:
        return None
    from .sitemap import parse_sitemap

    parsed = parse_sitemap(sitemap.data.decode("utf-8", errors="replace"))
    if parsed is None:
        return None
    return _classify(tuple(page.url for page in parsed))


async def _discover_by_bfs(
    root: str, config: CrawlConfig, context
) -> tuple[DiscoveredPage, ...]:
    visited: set[str] = set()
    frontier: set[str] = {root}
    discovered: set[str] = set()

    while frontier and len(visited) < config.max_pages:
        batch = tuple(sorted(frontier - visited))[: config.max_pages - len(visited)]
        if not batch:
            break
        for url in batch:
            try:
                page = await fetch_page(url, context, config)
                discovered.add(url)
                links = extract_links(page.html, page.url)
                internal, _ = classify_links(links, config.allowed_domains)
                frontier |= set(internal)
            except CrawlerError:
                pass
            visited.add(url)

    return _classify(tuple(sorted(discovered)))


async def discover(
    root_url: str, config: CrawlConfig, context=None
) -> DiscoveryResult:
    owns = context is None
    if owns:
        context, browser, playwright = await _open_browser(config)
    else:
        browser = playwright = None
    try:
        root = normalize_url(root_url)
        sitemap_pages = await _discover_by_sitemap(root, config, context)
        if sitemap_pages is not None:
            return DiscoveryResult(pages=sitemap_pages, source="SITEMAP")
        bfs_pages = await _discover_by_bfs(root, config, context)
        return DiscoveryResult(pages=bfs_pages, source="BFS_FALLBACK")
    finally:
        if owns:
            await _close_browser(context, browser, playwright)


def _error_document(page: DiscoveredPage, error: str) -> CrawlDocument:
    return CrawlDocument(
        url=page.url,
        title="",
        markdown="",
        html="",
        depth=0,
        status_code=0,
        error=error,
    )


async def _process(page: DiscoveredPage, config: CrawlConfig, context) -> CrawlDocument:
    if page.kind == "DOCUMENT":
        try:
            dl = await documents.download(page.url, context, config)
            return CrawlDocument(
                url=dl.url, title="", markdown="", html="", depth=0,
                status_code=200, content=dl.data, file_type=dl.file_type,
            )
        except CrawlerError as e:
            return _error_document(page, str(e))

    try:
        fetched = await fetch_page(page.url, context, config)
        markdown = _extract_markdown(fetched.html, fetched.url)
        title = _extract_title(fetched.html, fetched.url)
        links = extract_links(fetched.html, fetched.url)
        internal, external = classify_links(links, config.allowed_domains)
        return CrawlDocument(
            url=fetched.url, title=title, markdown=markdown, html=fetched.html,
            depth=0, status_code=fetched.status_code,
            internal_links=internal, external_links=external,
        )
    except CrawlerError as e:
        return _error_document(page, str(e))


async def crawl_confirmed(
    pages: tuple[DiscoveredPage, ...], config: CrawlConfig, context=None
) -> tuple[CrawlDocument, ...]:
    owns = context is None
    if owns:
        context, browser, playwright = await _open_browser(config)
    else:
        browser = playwright = None
    try:
        results: list[CrawlDocument] = []
        for page in pages:
            results.append(await _process(page, config, context))
            await asyncio.sleep(config.delay_between_requests)
        return tuple(results)
    finally:
        if owns:
            await _close_browser(context, browser, playwright)
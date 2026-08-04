import asyncio
import httpx
from .config import CrawlConfig
from .models import FetchedPage
from .exception import FetchError

async def fetch_page(url: str, client: httpx.AsyncClient, config: CrawlConfig) -> FetchedPage:
    async def attempt(remaining: int) -> FetchedPage:
        try:
            response = await client.get(
                url,
                timeout=config.request_timeout,
                follow_redirects=config.follow_redirects,
                headers={"User-Agent": config.user_agent},
            )
            return FetchedPage(url=str(response.url), status_code=response.status_code, html=response.text)
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as e:
            if remaining <= 0:
                raise FetchError(f"{url}: {e}") from e
            await asyncio.sleep(config.delay_between_requests)
            return await attempt(remaining - 1)

    return await attempt(config.retry_count)

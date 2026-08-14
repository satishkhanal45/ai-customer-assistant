"""Playwright fetch boundary for Crawler v2.

This module is the only network I/O boundary for page/document fetching. Two
entry points, mirroring the locked design:

* ``fetch_page``  — full page navigation (``page.goto`` + fixed timeout,
  decision 7); returns the same ``FetchedPage`` shape the rest of the pipeline
  expects (``html`` from ``page.content()``).
* ``fetch_bytes`` — plain HTTP via ``context.request`` (decision 12); never
  touches Chromium's page renderer. Used for robots.txt, sitemap.xml and
  document downloads.

Both implement retry-with-backoff (decision 10) against Playwright's error
surface (navigation timeout, page crash) rather than httpx exceptions.

A ``context`` (Playwright ``BrowserContext``) is passed in and owned by the
caller in ``crawler.py``; a fresh ``Page`` is created and closed per call so
state does not leak between navigations.
"""

import asyncio

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import CrawlConfig
from .exception import FetchError
from .models import FetchedBytes, FetchedPage

_TRANSIENT_ERRORS = (PlaywrightError, PlaywrightTimeoutError)


def _backoff_delay(attempt: int, config: CrawlConfig) -> float:
    base = config.delay_between_requests or 0.5
    return base * (2**attempt)


async def _wait_fixed_timeout(page, url: str, config: CrawlConfig) -> None:
    await page.goto(url, timeout=int(config.request_timeout * 1000))


async def _wait_networkidle(page, url: str, config: CrawlConfig) -> None:
    await page.goto(
        url, wait_until="networkidle", timeout=int(config.request_timeout * 1000)
    )


async def _wait_selector(page, url: str, config: CrawlConfig) -> None:
    await page.goto(url, timeout=int(config.request_timeout * 1000))
    await page.wait_for_selector(
        config.wait_selector, timeout=int(config.request_timeout * 1000)
    )


_WAIT_STRATEGIES = {
    "fixed_timeout": _wait_fixed_timeout,
    "networkidle": _wait_networkidle,
    "selector": _wait_selector,
}


async def fetch_page(
    url: str, context, config: CrawlConfig
) -> FetchedPage:
    """Navigate to ``url`` in a fresh page and return rendered HTML.

    The wait condition is selected from ``config.wait_strategy`` (decision 7);
    the retry/backoff handling below is identical for every strategy -- only
    the wait step differs. Invalid strategies are rejected at config
    construction, never here.
    """
    wait = _WAIT_STRATEGIES[config.wait_strategy]

    async def attempt(remaining: int) -> FetchedPage:
        page = await context.new_page()
        try:
            await wait(page, url, config)
            return FetchedPage(
                url=url,
                status_code=200,
                html=await page.content(),
            )
        except _TRANSIENT_ERRORS as e:
            if remaining <= 0:
                raise FetchError(f"{url}: {e}") from e
            await asyncio.sleep(_backoff_delay(config.retry_count - remaining, config))
            return await attempt(remaining - 1)
        finally:
            await page.close()

    return await attempt(config.retry_count)


async def fetch_bytes(
    url: str, context, config: CrawlConfig
) -> FetchedBytes:
    """Fetch raw response bytes over Playwright's plain ``context.request``."""

    async def attempt(remaining: int) -> FetchedBytes:
        try:
            response = await context.request.get(
                url,
                timeout=int(config.request_timeout * 1000),
                max_redirects=20 if config.follow_redirects else 0,
            )
            content_type = response.headers.get("content-type", "")
            return FetchedBytes(
                url=str(response.url),
                status_code=response.status,
                content_type=content_type,
                data=await response.body(),
            )
        except _TRANSIENT_ERRORS as e:
            if remaining <= 0:
                raise FetchError(f"{url}: {e}") from e
            await asyncio.sleep(_backoff_delay(config.retry_count - remaining, config))
            return await attempt(remaining - 1)

    return await attempt(config.retry_count)

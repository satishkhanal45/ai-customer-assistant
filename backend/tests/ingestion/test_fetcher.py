from types import SimpleNamespace

import pytest
from pytest_asyncio import fixture as async_fixture
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from ingestion.crawler.config import CrawlConfig
from ingestion.crawler.exception import FetchError
from ingestion.crawler.fetcher import fetch_bytes, fetch_page
from ingestion.crawler.models import FetchedBytes, FetchedPage

CONFIG = CrawlConfig(request_timeout=5.0, retry_count=2, delay_between_requests=0.0)


class FakeResponse:
    def __init__(self, url, status, content_type="", data=b""):
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._data = data

    async def body(self):
        return self._data


class FakeRequestClient:
    """Deterministic stand-in for Playwright's APIRequestContext.

    ``context.request`` is NOT intercepted by ``page.route()`` — Playwright's
    APIRequestContext bypasses page-level routing entirely (it's a separate
    plain-HTTP client). That is why the ``fetch_bytes`` path is stubbed with
    this fake here, while ``fetch_page`` (which navigates a real Page) is
    mocked with ``page.route()`` below. Keep these two mocking strategies
    separate; do not "simplify" the bytes tests onto ``page.route()``, as that
    would silently bypass the interception and hit the real network, losing
    coverage of the document-download path.
    """

    def __init__(self, plan=None):
        self.plan = list(plan or [])
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.plan.pop(0) if self.plan else FakeResponse(url, 200)
        if isinstance(item, BaseException):
            raise item
        return item


@async_fixture
async def context():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context()
    yield ctx
    await browser.close()
    await pw.stop()


@pytest.mark.asyncio
async def test_fetch_page_rendered_html_via_route(context):
    async def route_handler(route):
        await route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><body><h1>Hello</h1></body></html>",
        )

    await context.route("**/page", route_handler)
    page = await fetch_page("https://example.com/page", context, CONFIG)
    assert page == FetchedPage(
        url="https://example.com/page", status_code=200, html=page.html
    )
    assert "Hello" in page.html


@pytest.mark.asyncio
async def test_fetch_bytes_success():
    client = FakeRequestClient(
        [FakeResponse("https://example.com/doc.pdf", 200, "application/pdf", b"%PDF-1.4")]
    )
    fetched = await fetch_bytes(
        "https://example.com/doc.pdf", SimpleNamespace(request=client), CONFIG
    )
    assert fetched == FetchedBytes(
        url="https://example.com/doc.pdf",
        status_code=200,
        content_type="application/pdf",
        data=b"%PDF-1.4",
    )


@pytest.mark.asyncio
async def test_fetch_bytes_http_error_passthrough():
    client = FakeRequestClient([FakeResponse("https://example.com/missing", 404)])
    fetched = await fetch_bytes(
        "https://example.com/missing", SimpleNamespace(request=client), CONFIG
    )
    assert fetched.status_code == 404


@pytest.mark.asyncio
async def test_fetch_bytes_retries_then_succeeds():
    client = FakeRequestClient(
        [
            PlaywrightError("net::ERR_CONNECTION_REFUSED"),
            FakeResponse("https://example.com/retry", 200, "text/plain", b"ok"),
        ]
    )
    fetched = await fetch_bytes(
        "https://example.com/retry", SimpleNamespace(request=client), CONFIG
    )
    assert fetched.data == b"ok"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_fetch_bytes_gives_up_after_retries():
    client = FakeRequestClient([PlaywrightError("boom")] * 3)
    with pytest.raises(FetchError):
        await fetch_bytes(
            "https://example.com/dead", SimpleNamespace(request=client), CONFIG
        )

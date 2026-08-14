import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ingestion.crawler import crawler, documents
from ingestion.crawler.config import CrawlConfig, CrawlMode
from ingestion.crawler.exception import FetchError
from ingestion.crawler.models import (
    DiscoveredPage,
    DiscoveryResult,
    DocumentDownload,
    FetchedBytes,
    FetchedPage,
)

ROOT = "https://example.com/"
CONFIG = CrawlConfig(
    mode=CrawlMode.SITE,
    allowed_domains=("example.com",),
    max_pages=20,
    retry_count=2,
    delay_between_requests=0.0,
)

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/about</loc></url>
</urlset>
"""

SITEMAPINDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
</sitemapindex>
"""


def _fetch_bytes_stub(responses):
    async def fake_fetch_bytes(url, context, config):
        return responses.get(url, FetchedBytes(url, 404, "", b""))

    return fake_fetch_bytes


@pytest.mark.asyncio
async def test_discover_sitemap_path(monkeypatch):
    responses = {
        "https://example.com/robots.txt": FetchedBytes(
            "https://example.com/robots.txt", 200, "text/plain", b"Sitemap: https://example.com/sitemap.xml"
        ),
        "https://example.com/sitemap.xml": FetchedBytes(
            "https://example.com/sitemap.xml", 200, "application/xml", URLSET.encode()
        ),
    }
    monkeypatch.setattr(crawler, "fetch_bytes", _fetch_bytes_stub(responses))

    result = await crawler.discover(ROOT, CONFIG, context=object())
    assert result == DiscoveryResult(
        pages=(
            DiscoveredPage(url="https://example.com/", kind="PAGE"),
            DiscoveredPage(url="https://example.com/about", kind="PAGE"),
        ),
        source="SITEMAP",
    )


@pytest.mark.asyncio
async def test_discover_bfs_fallback_when_no_sitemap(monkeypatch):
    monkeypatch.setattr(crawler, "fetch_bytes", _fetch_bytes_stub({}))

    async def fake_fetch_page(url, context, config):
        html = (
            f'<html><a href="{ROOT}about">a</a>'
            f'<a href="https://external.com/x">x</a>'
            f'<a href="{ROOT}doc.pdf">d</a></html>'
        )
        return FetchedPage(url, 200, html)

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch_page)

    result = await crawler.discover(ROOT, CONFIG, context=object())
    assert result.source == "BFS_FALLBACK"
    urls = {p.url for p in result.pages}
    assert ROOT in urls
    assert ROOT + "about" in urls
    assert "https://external.com/x" not in urls
    doc = next(p for p in result.pages if p.url == ROOT + "doc.pdf")
    assert doc.kind == "DOCUMENT" and doc.file_type == "PDF"


@pytest.mark.asyncio
async def test_discover_sitemapindex_triggers_bfs_fallback(monkeypatch):
    responses = {
        "https://example.com/robots.txt": FetchedBytes(
            "https://example.com/robots.txt", 200, "text/plain", b"Sitemap: https://example.com/sitemap.xml"
        ),
        "https://example.com/sitemap.xml": FetchedBytes(
            "https://example.com/sitemap.xml", 200, "application/xml", SITEMAPINDEX.encode()
        ),
    }
    monkeypatch.setattr(crawler, "fetch_bytes", _fetch_bytes_stub(responses))

    async def fake_fetch_page(url, context, config):
        return FetchedPage(url, 200, f'<html><a href="{ROOT}home">a</a></html>')

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch_page)

    result = await crawler.discover(ROOT, CONFIG, context=object())
    assert result.source == "BFS_FALLBACK"


@pytest.mark.asyncio
async def test_crawl_confirmed_pages_and_documents(monkeypatch):
    pages = (
        DiscoveredPage(url=ROOT, kind="PAGE"),
        DiscoveredPage(url=ROOT + "doc.pdf", kind="DOCUMENT", file_type="PDF"),
    )

    async def fake_fetch_page(url, context, config):
        return FetchedPage(url, 200, "<html><body><p>content</p></body></html>")

    async def fake_download(url, context, config):
        return DocumentDownload(url, b"%PDF-1.4", "application/pdf", "PDF")

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(crawler, "_extract_markdown", lambda html, url: "extracted")
    monkeypatch.setattr(crawler, "_extract_title", lambda html, url: "Title")
    monkeypatch.setattr(documents, "download", fake_download)

    docs = await crawler.crawl_confirmed(pages, CONFIG, context=object())
    assert [d.url for d in docs] == [ROOT, ROOT + "doc.pdf"]
    assert docs[0].error is None and docs[0].markdown == "extracted"
    assert docs[1].file_type == "PDF" and docs[1].content == b"%PDF-1.4"


@pytest.mark.asyncio
async def test_crawl_confirmed_continues_after_page_failure(monkeypatch):
    pages = (
        DiscoveredPage(url=ROOT + "broken", kind="PAGE"),
        DiscoveredPage(url=ROOT + "ok", kind="PAGE"),
    )

    async def fake_fetch_page(url, context, config):
        if url.endswith("broken"):
            raise FetchError("boom")
        return FetchedPage(url, 200, "<html><body><p>fine</p></body></html>")

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(crawler, "_extract_markdown", lambda html, url: "extracted")
    monkeypatch.setattr(crawler, "_extract_title", lambda html, url: "Title")

    docs = await crawler.crawl_confirmed(pages, CONFIG, context=object())
    assert docs[0].error == "boom"
    assert docs[1].error is None
    assert docs[1].markdown == "extracted"


# ---------------------------------------------------------------------------
# Integration: full BFS-fallback discovery traversal against a real local
# fixture server (no sitemap), exercising frontier logic end-to-end. This is
# the one path only unit-tested at the branch level so far.
# ---------------------------------------------------------------------------

_SITE = {
    "/robots.txt": (404, "text/plain", b""),
    "/": (
        200,
        "text/html",
        b'<html><a href="/about">a</a><a href="/contact">c</a>'
        b'<a href="https://external.com/x">x</a></html>',
    ),
    "/about": (200, "text/html", b'<html><a href="/">home</a></html>'),
    "/contact": (200, "text/html", b"<html><p>no links</p></html>"),
}


class _SiteHandler(BaseHTTPRequestHandler):
    site = {}

    def do_GET(self):  # noqa: N802
        status, ctype, body = self.site.get(self.path, (404, "text/plain", b""))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logs
        pass


@pytest.fixture(scope="module")
def site_server():
    _SiteHandler.site = _SITE
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()


@pytest.mark.asyncio
async def test_discover_bfs_traversal_integration(monkeypatch, site_server):
    """End-to-end BFS fallback over a no-sitemap site.

    Confirms discovery is link-only (extract_markdown is never invoked, per
    design decision 4) and that the discovered page list matches the fixture's
    actual link graph, with external links excluded and source set correctly.
    """
    def boom(*args, **kwargs):
        raise AssertionError("extract_markdown must NOT run during discovery")

    monkeypatch.setattr(crawler, "_extract_markdown", boom)

    from urllib.parse import urlsplit

    netloc = urlsplit(site_server).netloc  # includes the non-default port
    config = CrawlConfig(
        mode=CrawlMode.SITE,
        allowed_domains=(netloc,),
        max_pages=10,
        request_timeout=10.0,
        delay_between_requests=0.0,
    )

    result = await crawler.discover(site_server + "/", config)

    assert result.source == "BFS_FALLBACK"
    urls = {p.url for p in result.pages}
    assert urls == {site_server + "/", site_server + "/about", site_server + "/contact"}
    assert all(p.kind == "PAGE" for p in result.pages)
    assert "https://external.com/x" not in urls

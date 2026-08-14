import pytest

from ingestion.crawler import documents
from ingestion.crawler.config import CrawlConfig
from ingestion.crawler.models import DiscoveredPage, DocumentDownload


def test_classify_url_by_extension():
    assert documents.classify_url("https://example.com/doc.pdf") == DiscoveredPage(
        url="https://example.com/doc.pdf", kind="DOCUMENT", file_type="PDF"
    )
    assert documents.classify_url("https://example.com/report.xlsx") == DiscoveredPage(
        url="https://example.com/report.xlsx", kind="DOCUMENT", file_type="XLSX"
    )


def test_classify_url_content_type_fallback():
    assert documents.classify_url(
        "https://example.com/blob", content_type="application/pdf"
    ) == DiscoveredPage(url="https://example.com/blob", kind="DOCUMENT", file_type="PDF")


def test_classify_url_extension_wins_over_content_type():
    page = documents.classify_url(
        "https://example.com/thing.docx", content_type="application/octet-stream"
    )
    assert page == DiscoveredPage(
        url="https://example.com/thing.docx", kind="DOCUMENT", file_type="DOCX"
    )


def test_classify_url_plain_page():
    assert documents.classify_url("https://example.com/") == DiscoveredPage(
        url="https://example.com/", kind="PAGE"
    )
    assert documents.classify_url(
        "https://example.com/", content_type="text/html"
    ) == DiscoveredPage(url="https://example.com/", kind="PAGE")


@pytest.mark.asyncio
async def test_download_returns_bytes_and_file_type(monkeypatch):
    async def fake_fetch_bytes(url, context, config):
        return type(
            "FB",
            (),
            {
                "url": url,
                "status_code": 200,
                "content_type": "application/pdf",
                "data": b"%PDF-1.4 fake",
            },
        )()

    monkeypatch.setattr(documents, "fetch_bytes", fake_fetch_bytes)
    dl = await documents.download(
        "https://example.com/doc.pdf", object(), CrawlConfig()
    )
    assert dl == DocumentDownload(
        url="https://example.com/doc.pdf",
        data=b"%PDF-1.4 fake",
        content_type="application/pdf",
        file_type="PDF",
    )


@pytest.mark.asyncio
async def test_download_raises_on_http_error(monkeypatch):
    async def fake_fetch_bytes(url, context, config):
        return type(
            "FB", (), {"url": url, "status_code": 404, "content_type": "", "data": b""}
        )()

    monkeypatch.setattr(documents, "fetch_bytes", fake_fetch_bytes)
    try:
        await documents.download("https://example.com/missing.pdf", object(), CrawlConfig())
    except documents.FetchError as e:
        assert "404" in str(e)
    else:
        raise AssertionError("expected FetchError")

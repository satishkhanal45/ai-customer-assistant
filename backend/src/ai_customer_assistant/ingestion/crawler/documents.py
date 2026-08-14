"""Document classification and download handoff.

``classify_url`` is a pure function: it decides whether a discovered URL is a
PAGE or a DOCUMENT from its extension and/or ``Content-Type``. ``download`` is
the only I/O in this module — it wraps ``fetcher.fetch_bytes`` (Playwright's
plain ``context.request``, never a page navigation) and returns raw bytes plus
the detected ``file_type`` for handoff to the ingestion/storage layer.

Persistence (checksum -> dedup -> version -> job) is out of scope here; this
module's job stops at "here are the bytes and the detected file_type".
"""

from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .config import CrawlConfig
from .exception import FetchError
from .fetcher import fetch_bytes
from .models import DiscoveredPage, DocumentDownload

# Map file extensions to knowledge_source_version.file_type values.
FILE_TYPES_BY_EXTENSION = {
    ".pdf": "PDF",
    ".docx": "DOCX",
    ".doc": "DOC",
    ".xlsx": "XLSX",
    ".xls": "XLS",
    ".pptx": "PPTX",
    ".ppt": "PPT",
    ".csv": "CSV",
    ".txt": "TXT",
}

# Map (lower-cased) Content-Type media types to file_type values.
FILE_TYPES_BY_CONTENT_TYPE = {
    "application/pdf": "PDF",
    "application/msword": "DOC",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.ms-excel": "XLS",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
    "application/vnd.ms-powerpoint": "PPT",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
    "text/csv": "CSV",
    "text/plain": "TXT",
}


def _extension_of(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


def _content_type_file_type(content_type: str) -> Optional[str]:
    media = content_type.split(";", 1)[0].strip().lower()
    return FILE_TYPES_BY_CONTENT_TYPE.get(media)


def classify_url(
    url: str, content_type: Optional[str] = None
) -> DiscoveredPage:
    """Classify a discovered URL as PAGE or DOCUMENT.

    Extension is checked first; ``Content-Type`` (e.g. from a HEAD request) is
    a fallback when the extension is not a recognised document type.
    """
    file_type = FILE_TYPES_BY_EXTENSION.get(_extension_of(url))
    if file_type is None and content_type:
        file_type = _content_type_file_type(content_type)
    if file_type is None:
        return DiscoveredPage(url=url, kind="PAGE")
    return DiscoveredPage(url=url, kind="DOCUMENT", file_type=file_type)


async def download(
    url: str, context, config: CrawlConfig
) -> DocumentDownload:
    """Fetch raw document bytes via Playwright's plain ``context.request``.

    ``context`` is a Playwright ``BrowserContext`` (the I/O boundary lives in
    ``fetcher.py``). Returns a ``DocumentDownload`` carrying the bytes and the
    detected ``file_type``; persistence is handled by a later stage.
    """
    fetched = await fetch_bytes(url, context, config)
    if fetched.status_code >= 400:
        raise FetchError(
            f"{url}: HTTP {fetched.status_code} during document download"
        )
    file_type = (
        FILE_TYPES_BY_EXTENSION.get(_extension_of(url))
        or _content_type_file_type(fetched.content_type)
        or "PDF"
    )
    return DocumentDownload(
        url=fetched.url,
        data=fetched.data,
        content_type=fetched.content_type,
        file_type=file_type,
    )

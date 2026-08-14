from dataclasses import dataclass
from typing import Literal, Optional

@dataclass(frozen=True)
class PageMeta:
    title: str
    canonical_url: str
    description: str

@dataclass(frozen=True)
class FetchedPage:
    url: str
    status_code: int
    html: str

@dataclass(frozen=True)
class FetchedBytes:
    url: str
    status_code: int
    content_type: str
    data: bytes

@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    kind: Literal["PAGE", "DOCUMENT"]
    file_type: Optional[str] = None

@dataclass(frozen=True)
class DiscoveryResult:
    pages: tuple[DiscoveredPage, ...]
    source: Literal["SITEMAP", "BFS_FALLBACK"]

@dataclass(frozen=True)
class DocumentDownload:
    url: str
    data: bytes
    content_type: str
    file_type: str

@dataclass(frozen=True)
class CrawlDocument:
    url: str
    title: str
    markdown: str
    html: str
    depth: int
    status_code: int
    internal_links: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()
    content: bytes = b""
    file_type: Optional[str] = None
    error: Optional[str] = None

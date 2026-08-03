from dataclasses import dataclass
from typing import Optional

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
class CrawlDocument:
    url: str
    title: str
    markdown: str
    html: str
    depth: int
    status_code: int
    internal_links: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()
    error: Optional[str] = None
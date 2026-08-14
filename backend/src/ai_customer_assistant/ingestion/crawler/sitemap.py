"""Pure sitemap/robots parsing. No network, no Playwright.

These functions only parse text/XML that has already been fetched by the I/O
boundary (``fetcher.py``). They are kept pure so they can be unit-tested with
fixture strings.

A ``<sitemapindex>`` root (a sitemap-of-sitemaps) is treated as unparseable
and returns ``None`` so the caller in ``crawler.py`` falls through to the BFS
fallback — child sitemaps are intentionally not recursed (decision 5).
"""

from typing import Optional
from xml.etree import ElementTree

from .models import DiscoveredPage


def _local_name(tag: str) -> str:
    """Strip the XML namespace off an element tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_robots_sitemap(robots_text: str) -> Optional[str]:
    """Return the URL from the first ``Sitemap:`` line in robots.txt text.

    Returns ``None`` when no usable ``Sitemap:`` directive is present.
    """
    for raw in robots_text.splitlines():
        line = raw.strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                return url
    return None


def parse_sitemap(xml_text: str) -> Optional[tuple[DiscoveredPage, ...]]:
    """Parse a flat ``<urlset>`` sitemap into ``DiscoveredPage`` entries.

    Returns ``None`` when the document is not a parseable flat page list (an
    empty document, a ``<sitemapindex>``, or an unknown root element) — the
    caller uses this to fall through to the BFS fallback path.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None

    name = _local_name(root.tag)
    if name != "urlset":
        return None

    pages: list[DiscoveredPage] = []
    for child in root:
        if _local_name(child.tag) != "url":
            continue
        loc = child.findtext("{*}loc") or child.findtext("loc")
        if loc and loc.strip():
            pages.append(DiscoveredPage(url=loc.strip(), kind="PAGE"))
    return tuple(pages)

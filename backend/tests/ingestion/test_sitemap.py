from ingestion.crawler.sitemap import parse_robots_sitemap, parse_sitemap
from ingestion.crawler.models import DiscoveredPage

ROBOTS_WITH_SITEMAP = """User-agent: *
Disallow: /private/

Sitemap: https://example.com/sitemap.xml
"""
ROBOTS_NO_SITEMAP = "User-agent: *\nDisallow: /private/\n"

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/about</loc></url>
</urlset>
"""

SITEMAPINDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>
</sitemapindex>
"""


def test_parse_robots_sitemap_finds_directive():
    assert parse_robots_sitemap(ROBOTS_WITH_SITEMAP) == "https://example.com/sitemap.xml"


def test_parse_robots_sitemap_case_insensitive():
    text = "Sitemap: http://example.com/sitemap_index.xml\n"
    assert parse_robots_sitemap(text) == "http://example.com/sitemap_index.xml"


def test_parse_robots_sitemap_none_when_absent():
    assert parse_robots_sitemap(ROBOTS_NO_SITEMAP) is None
    assert parse_robots_sitemap("") is None


def test_parse_sitemap_flat_urlset():
    pages = parse_sitemap(URLSET)
    assert pages == (
        DiscoveredPage(url="https://example.com/", kind="PAGE"),
        DiscoveredPage(url="https://example.com/about", kind="PAGE"),
    )


def test_parse_sitemapindex_is_unparseable():
    """A <sitemapindex> must NOT be partially parsed; fall through to BFS."""
    assert parse_sitemap(SITEMAPINDEX) is None


def test_parse_sitemap_invalid_xml_is_none():
    assert parse_sitemap("<urlset><url>") is None
    assert parse_sitemap("not xml at all") is None


def test_parse_sitemap_wrong_root_is_none():
    assert parse_sitemap("<html><body>hi</body></html>") is None

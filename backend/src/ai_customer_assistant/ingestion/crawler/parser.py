from lxml import html as lxml_html
from .utils import normalize_url, same_domain, is_crawlable_scheme

def _raw_hrefs(html_text: str) -> tuple[str, ...]:
    tree = lxml_html.fromstring(html_text)
    return tuple(tree.xpath("//a/@href"))

def extract_links(html_text: str, base_url: str) -> tuple[str, ...]:
    return tuple(
        normalize_url(href, base=base_url)
        for href in _raw_hrefs(html_text)
        if is_crawlable_scheme(href)
    )

def classify_links(
    links: tuple[str, ...], allowed_domains: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    internal = tuple(filter(lambda u: same_domain(u, allowed_domains), links))
    external = tuple(u for u in links if u not in internal)
    return internal, external
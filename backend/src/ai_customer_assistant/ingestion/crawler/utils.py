from urllib.parse import urlsplit, urlunsplit, urljoin

def normalize_url(url: str, base: str | None = None) -> str:
    absolute = urljoin(base, url) if base else url
    parts = urlsplit(absolute)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))

def same_domain(url: str, allowed_domains: tuple[str, ...]) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == d.lower() or host.endswith(f".{d.lower()}") for d in allowed_domains)

NON_CRAWLABLE_SCHEMES = ("mailto:", "tel:", "javascript:")

def is_crawlable_scheme(url: str) -> bool:
    return not url.startswith(NON_CRAWLABLE_SCHEMES)

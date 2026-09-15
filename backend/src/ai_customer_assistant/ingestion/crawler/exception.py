class CrawlerError(Exception):
    """Base class for crawler errors."""

class FetchError(CrawlerError):
    """Raised when a page cannot be downloaded after retries."""

class ExtractionError(CrawlerError):
    """Raised when Trafilatura fails to extract content."""

class BlockedURLError(FetchError):
    """Raised when a URL is refused by the SSRF guard (auth/ssrf.py).

    A subclass of FetchError so a link discovered mid-crawl is recorded as
    that one document failing rather than aborting the whole crawl -- a site
    that happens to link to an intranet host should not make the rest of it
    unindexable. It is a distinct class so that "we refused this" is never
    silently read back as "the site was slow".

    Deliberately NOT retried: the retry loop in fetcher.py catches Playwright
    errors only, so this propagates on the first attempt. Retrying a URL we
    have decided not to fetch would be an odd thing to do three times.
    """

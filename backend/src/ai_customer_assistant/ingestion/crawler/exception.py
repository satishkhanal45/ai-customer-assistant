class CrawlerError(Exception):
    """Base class for crawler errors."""

class FetchError(CrawlerError):
    """Raised when a page cannot be downloaded after retries."""

class ExtractionError(CrawlerError):
    """Raised when Trafilatura fails to extract content."""

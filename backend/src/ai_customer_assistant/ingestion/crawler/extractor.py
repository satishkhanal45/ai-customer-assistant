import trafilatura
from trafilatura.metadata import extract_metadata
from .models import PageMeta
from .exception import ExtractionError

def extract_markdown(html_text: str, url: str) -> str:
    markdown = trafilatura.extract(
        html_text,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        include_images=True,
        favor_precision=True,
    )
    if markdown is None:
        raise ExtractionError(f"Trafilatura could not extract content from {url}")
    return markdown

def extract_meta(html_text: str, url: str) -> PageMeta:
    meta = extract_metadata(html_text, default_url=url)
    return PageMeta(
        title=(meta.title if meta and meta.title else url),
        canonical_url=(meta.url if meta and meta.url else url),
        description=(meta.description if meta and meta.description else ""),
    )
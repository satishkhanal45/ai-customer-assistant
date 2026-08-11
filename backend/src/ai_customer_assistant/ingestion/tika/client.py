"""
Apache Tika integration (apache_tika_implementation_reference.md).

Design: everything that can be pure, is pure.
  - `build_headers` / `parse_extraction_response` have no side effects and
    are fully unit-testable without a running Tika server.
  - `extract_text` is the single I/O boundary: one PUT to /rmeta/text.

The pipeline never inspects Tika's raw JSON directly -- it only ever sees
the pure `ExtractedDocument` shape from pipeline_types.py.
"""

from __future__ import annotations

import httpx

from ingestion.pipeline_types import ExtractedDocument
from ingestion.tika.config import TikaSettings


class TikaExtractionError(Exception):
    """Raised for genuine parse failures (corrupted file, unsupported type)."""


class TikaTransientError(Exception):
    """Raised for retryable failures (timeout, 5xx, container overloaded)."""


def build_headers(content_type: str) -> dict[str, str]:
    """Pure: the exact header set Tika expects for /rmeta/text."""
    return {"Content-Type": content_type, "Accept": "application/json"}


def parse_extraction_response(payload: list[dict]) -> ExtractedDocument:
    """
    Pure: turn Tika's always-a-list JSON body into ExtractedDocument.

    Tika returns one entry per embedded object; the top-level document is
    always index 0. We deliberately do not index further into the list --
    embedded-content handling is a future concern, noted in section 3 of
    the reference doc, and is out of scope for this pipeline stage.
    """
    if not payload:
        raise TikaExtractionError("Tika returned an empty result list")

    top_level, *_embedded = payload
    text = (top_level.get("X-TIKA:content") or "").strip()
    return ExtractedDocument(text=text, metadata=top_level)


def extract_text(
    *,
    client: httpx.Client,
    settings: TikaSettings,
    raw_bytes: bytes,
    content_type: str,
) -> ExtractedDocument:
    """
    The single I/O call to Tika. Everything else in this module is pure;
    this function is the thin, replaceable-in-tests adapter around it.
    """
    if len(raw_bytes) > settings.max_file_size_bytes:
        raise TikaExtractionError(
            f"file exceeds max_file_size_bytes ({settings.max_file_size_bytes})"
        )

    try:
        response = client.put(
            settings.extract_url,
            content=raw_bytes,
            headers=build_headers(content_type),
            timeout=settings.request_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise TikaTransientError(str(exc)) from exc
    except httpx.TransportError as exc:
        raise TikaTransientError(str(exc)) from exc

    if response.status_code >= 500:
        raise TikaTransientError(f"Tika returned {response.status_code}")
    if response.status_code >= 400:
        raise TikaExtractionError(f"Tika rejected the file: {response.status_code}")

    return parse_extraction_response(response.json())

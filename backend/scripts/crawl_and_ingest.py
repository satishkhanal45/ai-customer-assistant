#!/usr/bin/env python
"""
Crawl a single URL and ingest it end-to-end.

    uv run --project backend python scripts/crawl_and_ingest.py https://example.com/handbook
    uv run --project backend python scripts/crawl_and_ingest.py https://example.com/handbook.pdf

Routing is decided purely by the fetched Content-Type (a pure function,
`classify_content_type`, see below) -- not by file extension:

  * text/html                          -> ingestion.crawler.Crawler (mode=PAGE)
                                           fetches + converts to markdown via
                                           trafilatura; the markdown is then
                                           registered as an MD version and
                                           flows through the SAME pipeline as
                                           everything else (Tika passes
                                           markdown through essentially
                                           unparsed, per the Tika reference
                                           doc section 6 -- no separate
                                           no-Tika code path needed).
  * pdf / doc / docx / ppt / pptx /
    xls / xlsx (any Office or PDF type) -> raw bytes are registered as-is;
                                           Tika extracts the text during
                                           ingestion, same as an uploaded file.

Either branch ends with one queued `knowledge_injection_job`, which this
script then runs immediately through the same `run_ingestion` the
background worker uses -- so a one-shot CLI call gives you a fully indexed
document without needing the worker running.

CONFIRMED against the real crawler.py / queues.py / io_output.py:
io_output.py only writes markdown to local disk -- it does NOT store to
MinIO or queue a job. That means the "crawler already stores to MinIO and
queues a job" README note refers to some other code path not seen here;
this script does the store+queue step itself, via
ingestion.queue.document_producer.register_document_version, reused for
both the HTML/markdown path and the PDF/Office path.

STILL AN ASSUMPTION: exact required/default fields of
ingestion.crawler.config.CrawlConfig beyond `mode` -- confirm it has
sensible defaults for concurrent_requests/max_pages/max_depth/
allowed_domains (unused in PAGE mode, but may still need defaults at
construction time).

NOTE: CrawlMode.PAGE returns exactly one CrawlDocument. CrawlMode.SITE
would return many (a full site crawl) -- ingesting all of them as separate
sources isn't handled here; ask if you want a --mode site option added.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # backend/.env

# Add src/ai_customer_assistant to sys.path so bare `ingestion.xxx` / `db.xxx`
# imports work when this script is invoked directly (matches pyproject.toml's
# pytest pythonpath convention, and how alembic/env.py resolves `db.models`).
_PKG_ROOT = Path(__file__).resolve().parent.parent / "src" / "ai_customer_assistant"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingestion.pipeline_types import FileType, JobType
from ingestion.queue.document_producer import register_document_version

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data layer: the closed vocabulary Tika/Office/PDF mime types map onto.
# schema.md's file_type enum only has PDF | DOCX | MD -- everything else
# maps to None (allowed, nullable) and keeps its real type in mime_type.
# ---------------------------------------------------------------------------

_DOCUMENT_MIME_TO_FILE_TYPE: dict[str, FileType | None] = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "application/msword": None,  # legacy .doc -- no enum value, see NOTE above
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": None,  # .pptx
    "application/vnd.ms-powerpoint": None,  # .ppt
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,  # .xlsx
    "application/vnd.ms-excel": None,  # .xls
}

RouteKind = Literal["html", "document"]


def classify_content_type(content_type: str) -> RouteKind:
    """Pure: decide which ingestion path a fetched Content-Type takes."""
    base_type = content_type.split(";", 1)[0].strip().lower()
    return "document" if base_type in _DOCUMENT_MIME_TO_FILE_TYPE else "html"


def resolve_file_type(content_type: str) -> FileType | None:
    """Pure: map a document mime type to the schema's (limited) enum."""
    base_type = content_type.split(";", 1)[0].strip().lower()
    return _DOCUMENT_MIME_TO_FILE_TYPE.get(base_type)


@dataclass(frozen=True, slots=True)
class FetchedUrl:
    url: str
    content_type: str
    raw_bytes: bytes


def fetch_url(url: str, *, client: httpx.Client) -> FetchedUrl:
    """
    The single I/O call that decides routing (HTML vs document). Kept as a
    plain httpx call rather than the crawler's own fetcher, since this is
    only used to sniff Content-Type before deciding whether to hand off to
    Crawler at all -- Crawler does its own fetch once we know it's HTML.
    """
    response = client.get(url, follow_redirects=True, timeout=30.0)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "application/octet-stream")
    return FetchedUrl(url=url, content_type=content_type, raw_bytes=response.content)


# ---------------------------------------------------------------------------
# HTML path -- real Crawler API confirmed: Crawler(config).crawl(url) ->
# tuple[CrawlDocument, ...]. io_output.py only saves to local disk, so the
# MinIO + queue step happens here, reusing document_producer with file_type=MD.
# ---------------------------------------------------------------------------


async def _run_html_crawler_pipeline(
    session: AsyncSession, fetched: FetchedUrl, *, uploaded_by: UUID, category_id: UUID | None
) -> UUID | None:
    from ingestion.crawler.config import CrawlConfig, CrawlMode
    from ingestion.crawler.crawler import Crawler

    config = CrawlConfig(mode=CrawlMode.PAGE)  # single page, not a recursive site crawl
    documents = await Crawler(config).crawl(fetched.url)

    doc = documents[0] if documents else None
    if doc is None or doc.error is not None:
        logger.error("crawl failed for %s: %s", fetched.url, doc.error if doc else "no document returned")
        return None

    job = await register_document_version(
        session,
        url=doc.url,
        raw_bytes=doc.markdown.encode("utf-8"),
        mime_type="text/markdown",
        file_type=FileType.MD,
        uploaded_by=uploaded_by,
        category_id=category_id,
    )
    return job.job_id if job is not None else None


# ---------------------------------------------------------------------------
# Document (PDF/Office) path -- new code, doesn't touch the crawler package.
# ---------------------------------------------------------------------------


async def _run_document_pipeline(
    session: AsyncSession, fetched: FetchedUrl, *, uploaded_by: UUID, category_id: UUID | None
) -> UUID | None:
    job = await register_document_version(
        session,
        url=fetched.url,
        raw_bytes=fetched.raw_bytes,
        mime_type=fetched.content_type,
        file_type=resolve_file_type(fetched.content_type),
        uploaded_by=uploaded_by,
        category_id=category_id,
    )
    return job.job_id if job is not None else None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# route -> handler. Both now share the same signature (session, fetched, *,
# uploaded_by, category_id) -> UUID | None, since both end up going through
# register_document_version -- replaces an if/elif on `route`.
_ROUTES = {
    "html": _run_html_crawler_pipeline,
    "document": _run_document_pipeline,
}


async def crawl_and_ingest(
    url: str, *, uploaded_by: UUID, category_id: UUID | None, session_factory: async_sessionmaker
) -> None:
    with httpx.Client() as http_client:
        fetched = fetch_url(url, client=http_client)

    route = classify_content_type(fetched.content_type)
    logger.info("routing %s as %s (content-type: %s)", url, route, fetched.content_type)

    async with session_factory() as session:
        job_id = await _ROUTES[route](session, fetched, uploaded_by=uploaded_by, category_id=category_id)

    if job_id is None:
        # The real reason was already logged at its source: either the ERROR
        # from _run_html_crawler_pipeline (crawl/extraction failure), or the
        # INFO below from register_document_version (genuine checksum dedup).
        # No generic guess here -- that's what caused the misleading message.
        return

    # Run the job immediately rather than waiting for the background
    # worker, so this CLI call fully ingests before it exits.
    from db.models import KnowledgeInjectionJob
    from ingestion.pipeline import run_ingestion
    from ingestion.pipeline_types import JobRef, JobStatus
    from ingestion.queue import repository as job_repo

    async with session_factory() as session:
        job_row = await session.get(KnowledgeInjectionJob, job_id)
        job = JobRef(
            job_id=job_row.job_id,
            source_id=job_row.source_id,
            version_id=job_row.version_id,
            job_type=JobType(job_row.job_type),
            status=JobStatus(job_row.status),
            triggered_by=job_row.triggered_by,
        )
        try:
            outcome = await run_ingestion(session, job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ingestion crashed for %s", url)
            await job_repo.complete_job(
                session,
                job_id=job.job_id,
                status=JobStatus.FAILED,
                chunks_created_count=0,
                entities_created_count=0,
                error_details=f"unhandled_exception: {exc}",
            )
            return

    logger.info(
        "ingestion %s for %s: chunks=%d entities=%d %s",
        outcome.status.value,
        url,
        outcome.chunks_created_count,
        outcome.entities_created_count,
        outcome.error_details or "",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="URL to crawl and ingest")
    parser.add_argument("--uploaded-by", required=True, help="app_user.id (a UUID) of the service account")
    parser.add_argument("--category-id", default=None, help="knowledge_category.category_id, optional")
    return parser.parse_args()


def _async_database_url() -> str:
    """TODO: confirm the real async driver name -- guessing
    `psycopg_async` here (SQLAlchemy's asyncio dialect for psycopg3).
    If your driver is actually asyncpg instead, this needs to produce
    `postgresql+asyncpg://` and you'd need the `asyncpg` package installed."""
    from db.session import database_url  # read-only import, not modified

    sync_url = database_url()
    return sync_url.replace("postgresql+psycopg://", "postgresql+psycopg_async://")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Quiet third-party libraries' own INFO logging (huggingface_hub's model
    # cache checks, httpx's per-request logging for every HF/Tika/Groq call)
    # -- keeps the log to your app's own ingestion progress lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    logging.getLogger("filelock").setLevel(logging.WARNING)
    args = _parse_args()

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_async_database_url())
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    asyncio.run(
        crawl_and_ingest(
            args.url,
            uploaded_by=UUID(args.uploaded_by),
            category_id=UUID(args.category_id) if args.category_id else None,
            session_factory=session_factory,
        )
    )


if __name__ == "__main__":
    main()

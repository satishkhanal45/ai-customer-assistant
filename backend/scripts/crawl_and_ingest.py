#!/usr/bin/env python
"""
Crawl a single URL and ingest it end-to-end.

    uv run --project backend python scripts/crawl_and_ingest.py https://example.com/handbook
    uv run --project backend python scripts/crawl_and_ingest.py https://example.com/handbook.pdf
    uv run --project backend python scripts/crawl_and_ingest.py https://example.com --scope site

Routing is decided purely by the fetched Content-Type (a pure function,
`classify_content_type`, see below) -- not by file extension:

  * text/html  with --scope page (default) -> crawl ONLY the given URL (no
                                            discovery), convert to markdown via
                                            trafilatura, register as MD, and
                                            flow through the SAME pipeline as
                                            everything else (Tika passes
                                            markdown through essentially
                                            unparsed, per the Tika reference
                                            doc section 6).
              with --scope site            -> ingestion.crawler.discover()
                                            then crawl_confirmed(): sitemap-
                                            first discovery, then renders each
                                            confirmed page to markdown; any
                                            non-HTML artifacts discovery turns
                                            up (e.g. a linked PDF) are
                                            downloaded and registered as
                                            documents, same as an upload.
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

NOTE: with --scope site, discovery can return many pages for a single root
URL (a full site crawl via sitemap or BFS fallback) -- this script ingests
every successfully crawled item as a separate source. The default --scope
page crawls only the given URL.
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
# HTML path -- two-phase crawler API: discover() then crawl_confirmed().
# io_output.py only saves to local disk, so the MinIO + queue step happens
# here, reusing document_producer with file_type=MD for pages and the
# document's own file_type for any non-HTML artifact discovery returns.
# ---------------------------------------------------------------------------

# file_type strings from ingestion.crawler.documents.classify_url map onto the
# schema's (limited) enum; unsupported ones stay None (nullable, like the
# document-upload path) and keep their real type in mime_type.
_CRAWL_FILE_TYPE_TO_ENUM: dict[str, FileType | None] = {
    "PDF": FileType.PDF,
    "DOCX": FileType.DOCX,
}
_CRAWL_FILE_TYPE_TO_MIME: dict[str, str] = {
    "PDF": "application/pdf",
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _crawl_file_type(file_type: str | None) -> FileType | None:
    return _CRAWL_FILE_TYPE_TO_ENUM.get(file_type) if file_type else None


def _crawl_mime(file_type: str | None) -> str:
    return _CRAWL_FILE_TYPE_TO_MIME.get(file_type, "application/octet-stream") if file_type else "text/markdown"


async def _run_html_crawler_pipeline(
    session: AsyncSession,
    fetched: FetchedUrl,
    *,
    uploaded_by: UUID,
    category_id: UUID | None,
    mode,  # ingestion.crawler.config.CrawlMode
) -> tuple[UUID, ...]:
    from urllib.parse import urlsplit
    from ingestion.crawler.config import CrawlConfig
    from ingestion.crawler.crawler import Crawler

    # Constrain the crawl to the starting URL's own domain, same as
    # crawler/__main__.py's _config_for -- otherwise CrawlMode.SITE will
    # follow every external link it finds.
    domain = urlsplit(fetched.url).netloc
    config = CrawlConfig(mode=mode, allowed_domains=(domain,))
    documents = await Crawler(config).crawl(fetched.url)

    if not documents:
        logger.error("crawl failed for %s: no documents returned", fetched.url)
        return ()

    job_ids: list[UUID] = []
    for doc in documents:
        if doc.error is not None:
            logger.error("crawl failed for %s: %s", doc.url, doc.error)
            continue
        job = await register_document_version(
            session,
            url=doc.url,
            raw_bytes=doc.content if doc.file_type else doc.markdown.encode("utf-8"),
            mime_type=_crawl_mime(doc.file_type),
            file_type=FileType.MD if not doc.file_type else _crawl_file_type(doc.file_type),
            uploaded_by=uploaded_by,
            category_id=category_id,
        )
        if job is not None:
            job_ids.append(job.job_id)
    return job_ids

    return tuple(job_ids)

# ---------------------------------------------------------------------------
# Document (PDF/Office) path -- new code, doesn't touch the crawler package.
# ---------------------------------------------------------------------------


async def _run_document_pipeline(
    session: AsyncSession, fetched: FetchedUrl, *, uploaded_by: UUID, category_id: UUID | None, mode
) -> tuple[UUID, ...]:
    job = await register_document_version(
        session,
        url=fetched.url,
        raw_bytes=fetched.raw_bytes,
        mime_type=fetched.content_type,
        file_type=resolve_file_type(fetched.content_type),
        uploaded_by=uploaded_by,
        category_id=category_id,
    )
    return (job.job_id,) if job is not None else ()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# route -> handler. Both share the same signature (session, fetched, *,
# uploaded_by, category_id) -> list[UUID] (job ids to run), since both end up
# going through register_document_version -- replaces an if/elif on `route`.
_ROUTES = {
    "html": _run_html_crawler_pipeline,
    "document": _run_document_pipeline,
}

async def crawl_and_ingest(
    url: str,
    *,
    uploaded_by: UUID,
    category_id: UUID | None,
    session_factory: async_sessionmaker,
    site: bool = False,
) -> None:
    from ingestion.crawler.config import CrawlMode

    with httpx.Client() as http_client:
        fetched = fetch_url(url, client=http_client)

    route = classify_content_type(fetched.content_type)
    mode = CrawlMode.SITE if site else CrawlMode.PAGE
    logger.info("routing %s as %s (content-type: %s, mode: %s)", url, route, fetched.content_type, mode)

    async with session_factory() as session:
        job_ids = await _ROUTES[route](session, fetched, uploaded_by=uploaded_by, category_id=category_id, mode=mode)

    if not job_ids:
        return


async def _run_job(
    session_factory: async_sessionmaker, job_id: UUID, url: str
) -> None:
    """Run one queued job immediately (mirrors the background worker)."""
    from db.models import KnowledgeInjectionJob
    from ingestion.pipeline import run_ingestion
    from ingestion.pipeline_types import JobRef, JobStatus
    from ingestion.queue import repository as job_repo

    logger.info("%d page(s) queued from %s -- ingesting each now", len(job_ids), url)

    for job_id in job_ids:
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
                logger.exception("ingestion crashed for job %s", job_id)
                await job_repo.complete_job(
                    session,
                    job_id=job.job_id,
                    status=JobStatus.FAILED,
                    chunks_created_count=0,
                    entities_created_count=0,
                    error_details=f"unhandled_exception: {exc}",
                )
                continue

        logger.info(
            "ingestion %s for job %s: chunks=%d entities=%d %s",
            outcome.status.value, job_id, outcome.chunks_created_count,
            outcome.entities_created_count, outcome.error_details or "",
        )

async def crawl_and_ingest(
    url: str,
    *,
    uploaded_by: UUID,
    category_id: UUID | None,
    session_factory: async_sessionmaker,
    scope: str = "page",
) -> None:
    with httpx.Client() as http_client:
        fetched = fetch_url(url, client=http_client)

    route = classify_content_type(fetched.content_type)
    logger.info("routing %s as %s (content-type: %s, scope: %s)", url, route, fetched.content_type, scope)

    async with session_factory() as session:
        job_ids = await _ROUTES[route](
            session, fetched, uploaded_by=uploaded_by, category_id=category_id, scope=scope
        )

    # Empty means every discovered item was either dedup-skipped or failed to
    # crawl. The real reason was already logged at its source -- either the
    # ERROR from _run_html_crawler_pipeline (crawl/extraction failure), or the
    # INFO from register_document_version (genuine checksum dedup). No generic
    # guess here -- that's what caused the misleading message before.
    for job_id in job_ids:
        await _run_job(session_factory, job_id, url)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", action="store_true", help="crawl the entire site (CrawlMode.SITE), not just this one page",)
    parser.add_argument("url", help="URL to crawl and ingest")
    parser.add_argument(
        "--scope",
        choices=["page", "site"],
        default="page",
        help="page = crawl only the given URL (default); site = sitemap-first discovery over the whole site",
    )
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
            site=args.site,
            scope=args.scope,
        )
    )


if __name__ == "__main__":
    main()

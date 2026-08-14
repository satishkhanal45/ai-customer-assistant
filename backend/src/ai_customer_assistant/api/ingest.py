"""HTTP ingestion endpoints.

Both paths reuse the exact orchestration the CLI worker uses
(``scripts/crawl_and_ingest.py``): register a document version (checksum
dedup, MinIO storage, queue row) then run ``run_ingestion`` immediately so
the result is indexable without a separate worker process.

  POST /ingest/upload            multipart file (PDF / DOCX / Markdown)
  POST /ingest/crawl             JSON {"url", "scope"} — single page by
                                 default (scope=PAGE). scope=SITE returns a
                                 discovery list for review instead of crawling.
  POST /ingest/crawl/discover    JSON {"root_url"} — sitemap-first discovery,
                                 returns a cached discovery_id for review.
  POST /ingest/crawl/{id}/confirm  runs crawl_confirmed() against the cached
                                 discovery and ingests each confirmed item.

Uploaded-by defaults to the system service account
(``00000000-0000-0000-0000-000000000000``); override with
``INGEST_DEFAULT_USER_ID`` in the environment.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.async_session import get_session, session_factory
from ingestion.crawler.config import CrawlConfig, CrawlMode
from ingestion.crawler.models import DiscoveryResult
from ingestion.pipeline_types import FileType, JobType, JobRef, JobStatus
from ingestion.queue.document_producer import register_document_version

router = APIRouter(prefix="/ingest", tags=["ingest"])

DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000000")

# Review data is cached server-side (not persisted) just long enough for a
# human/UI to look at the discovery list and then confirm it.
_DISCOVERY_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class _CachedDiscovery:
    created_at: float
    result: DiscoveryResult
    config: CrawlConfig


_discovery_cache: dict[str, _CachedDiscovery] = {}


def _cache_discovery(result: DiscoveryResult, config: CrawlConfig) -> str:
    discovery_id = uuid.uuid4().hex
    _discovery_cache[discovery_id] = _CachedDiscovery(
        created_at=time.monotonic(), result=result, config=config
    )
    return discovery_id


def _evict_expired() -> None:
    now = time.monotonic()
    expired = [
        key
        for key, cached in _discovery_cache.items()
        if now - cached.created_at > _DISCOVERY_TTL_SECONDS
    ]
    for key in expired:
        _discovery_cache.pop(key, None)


def _get_discovery(discovery_id: str) -> _CachedDiscovery | None:
    _evict_expired()
    return _discovery_cache.get(discovery_id)


def _uploaded_by() -> UUID:
    raw = os.environ.get("INGEST_DEFAULT_USER_ID")
    return UUID(raw) if raw else DEFAULT_USER_ID


_UPLOAD_MIME_TO_FILE_TYPE: dict[str, FileType] = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "text/markdown": FileType.MD,
}

_CRAWL_DOC_MIME_TO_FILE_TYPE: dict[str, FileType | None] = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "application/msword": None,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": None,
    "application/vnd.ms-powerpoint": None,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,
    "application/vnd.ms-excel": None,
}

RouteKind = Literal["html", "document"]


def _classify_content_type(content_type: str) -> RouteKind:
    base = content_type.split(";", 1)[0].strip().lower()
    return "document" if base in _CRAWL_DOC_MIME_TO_FILE_TYPE else "html"


def _resolve_file_type(content_type: str) -> FileType | None:
    base = content_type.split(";", 1)[0].strip().lower()
    return _CRAWL_DOC_MIME_TO_FILE_TYPE.get(base)


async def _run_job(job_id: UUID) -> None:
    """Execute a queued ingestion job synchronously (mirrors the CLI worker)."""
    from db.models import KnowledgeInjectionJob
    from ingestion.pipeline import run_ingestion
    from ingestion.queue import repository as job_repo

    async with session_factory() as session:
        row = await session.get(KnowledgeInjectionJob, job_id)
        if row is None:
            return
        job = JobRef(
            job_id=row.job_id,
            source_id=row.source_id,
            version_id=row.version_id,
            job_type=JobType(row.job_type),
            status=JobStatus(row.status),
            triggered_by=row.triggered_by,
        )
        try:
            await run_ingestion(session, job)
        except Exception as exc:  # noqa: BLE001
            await job_repo.complete_job(
                session,
                job_id=job.job_id,
                status=JobStatus.FAILED,
                chunks_created_count=0,
                entities_created_count=0,
                error_details=f"unhandled_exception: {exc}",
            )


async def _register_and_run(
    session: AsyncSession,
    *,
    url: str,
    raw_bytes: bytes,
    mime_type: str,
    file_type: FileType | None,
    category_id: UUID | None,
) -> dict:
    job = await register_document_version(
        session,
        url=url,
        raw_bytes=raw_bytes,
        mime_type=mime_type,
        file_type=file_type,
        uploaded_by=_uploaded_by(),
        category_id=category_id,
    )
    if job is None:
        return {"status": "duplicate_skipped"}
    await _run_job(job.job_id)
    return {
        "status": "submitted",
        "job_id": str(job.job_id),
        "source_id": str(job.source_id),
        "version_id": str(job.version_id),
    }


@router.post("/upload")
async def upload(
    file: UploadFile,
    category_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    mime = (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    file_type = _UPLOAD_MIME_TO_FILE_TYPE.get(mime)
    if file_type is None:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type '{mime}'. Expected PDF, DOCX or Markdown.",
        )
    name = (file.filename or "upload").rsplit("/", 1)[-1]
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    return await _register_and_run(
        session,
        url=f"manual_upload://{name}",
        raw_bytes=data,
        mime_type=mime,
        file_type=file_type,
        category_id=category_id,
    )


class CrawlRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=2048)
    category_id: UUID | None = None
    # PAGE (default) crawls only the given URL — no discovery, no review step.
    # SITE returns a discovery list for review instead of crawling in the same
    # request; the caller then POSTs /crawl/{discovery_id}/confirm.
    scope: Literal["PAGE", "SITE"] = "PAGE"


class DiscoverRequest(BaseModel):
    root_url: str = Field(..., min_length=5, max_length=2048)


class ConfirmRequest(BaseModel):
    category_id: UUID | None = None


# file_type strings produced by ingestion.crawler.documents.classify_url map
# onto the schema's (limited) FileType enum and a real MIME type for storage.
_FILE_TYPE_TO_ENUM: dict[str, FileType | None] = {
    "PDF": FileType.PDF,
    "DOCX": FileType.DOCX,
}
_FILE_TYPE_TO_MIME: dict[str, str] = {
    "PDF": "application/pdf",
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _crawl_file_type(file_type: str | None) -> FileType | None:
    return _FILE_TYPE_TO_ENUM.get(file_type) if file_type else None


def _crawl_mime(file_type: str | None) -> str:
    return _FILE_TYPE_TO_MIME.get(file_type, "application/octet-stream") if file_type else "text/markdown"


async def _run_discovery(root_url: str) -> tuple[DiscoveryResult, CrawlConfig]:
    from urllib.parse import urlsplit

    from ingestion.crawler.crawler import discover

    host = urlsplit(root_url).netloc
    config = CrawlConfig(mode=CrawlMode.SITE, allowed_domains=(host,))
    result = await discover(root_url, config)
    return result, config


def _review_payload(discovery_id: str, result: DiscoveryResult) -> dict:
    return {
        "discovery_id": discovery_id,
        "source": result.source,
        "page_count": sum(1 for p in result.pages if p.kind == "PAGE"),
        "document_count": sum(1 for p in result.pages if p.kind == "DOCUMENT"),
        "pages": [
            {"url": p.url, "kind": p.kind, "file_type": p.file_type}
            for p in result.pages
        ],
    }


async def _ingest_documents(
    session: AsyncSession, documents, category_id: UUID | None
) -> dict:
    """Register + run ingestion for every successfully crawled item."""
    outcomes = []
    for doc in documents:
        if doc.error is not None:
            outcomes.append({"url": doc.url, "status": "failed", "error": doc.error})
            continue
        outcome = await _register_and_run(
            session,
            url=doc.url,
            raw_bytes=doc.content if doc.file_type else doc.markdown.encode("utf-8"),
            mime_type=_crawl_mime(doc.file_type),
            file_type=FileType.MD if not doc.file_type else _crawl_file_type(doc.file_type),
            category_id=category_id,
        )
        outcome["url"] = doc.url
        outcomes.append(outcome)
    return {"status": "submitted", "results": outcomes}


async def _crawl_single_page(req: CrawlRequest, session: AsyncSession) -> dict:
    """PAGE scope: crawl exactly the given URL (v1 behavior), no discovery."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(req.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")
            raw_bytes = response.content
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}") from exc

    if _classify_content_type(content_type) == "document":
        return await _register_and_run(
            session,
            url=req.url,
            raw_bytes=raw_bytes,
            mime_type=content_type,
            file_type=_resolve_file_type(content_type),
            category_id=req.category_id,
        )

    from urllib.parse import urlsplit

    from ingestion.crawler.crawler import crawl_confirmed
    from ingestion.crawler.documents import classify_url

    host = urlsplit(req.url).netloc
    config = CrawlConfig(mode=CrawlMode.SITE, allowed_domains=(host,))
    page = classify_url(req.url)
    documents = await crawl_confirmed((page,), config)
    return await _ingest_documents(session, documents, req.category_id)


@router.post("/crawl")
async def crawl(
    req: CrawlRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if req.scope == "PAGE":
        return await _crawl_single_page(req, session)

    # SITE scope: discovery-first, always goes through a review step before any
    # crawling happens. No same-request auto-crawl.
    result, config = await _run_discovery(req.url)
    discovery_id = _cache_discovery(result, config)
    return _review_payload(discovery_id, result)


@router.post("/crawl/discover")
async def crawl_discover(
    req: DiscoverRequest,
) -> dict:
    result, config = await _run_discovery(req.root_url)
    discovery_id = _cache_discovery(result, config)
    return _review_payload(discovery_id, result)


@router.post("/crawl/{discovery_id}/confirm")
async def crawl_confirm(
    discovery_id: str,
    req: ConfirmRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from ingestion.crawler.crawler import crawl_confirmed

    cached = _get_discovery(discovery_id)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired discovery_id. Run /crawl/discover again.",
        )
    documents = await crawl_confirmed(cached.result.pages, cached.config)
    return await _ingest_documents(session, documents, req.category_id)
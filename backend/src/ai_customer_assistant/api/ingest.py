"""HTTP ingestion endpoints.

Every path here **enqueues only**. Registering a document (checksum dedup,
MinIO storage, queue row) is fast and happens inline; the actual ingestion
-- Tika extraction, the 400 MB embedding model, LLM entity extraction -- is
picked up by the worker process (``scripts/run_worker.py``, run as the
``worker`` service in docker-compose).

These endpoints used to `asyncio.create_task(_run_job(...))` and run the
pipeline inside the web process. That was wrong in four separate ways: the
task reference was dropped so it could be garbage-collected mid-run, there
was no concurrency limit so N uploads meant N concurrent pipelines, the
heavy model ran inside the request worker, and a restart orphaned in-flight
jobs as permanently RUNNING rows that blocked the source forever. It also
duplicated the worker's execution path, so the same job could be run twice.

Callers get ``202 Accepted`` with a ``job_id`` and poll
``GET /ingest/jobs/{job_id}`` for the outcome -- which the frontend already
did, because the work was always effectively asynchronous anyway.

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
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import Principal, enforce_ingest_quota, require_member
from auth.ssrf import UnsafeURLError, assert_url_is_safe, safe_get
from db.engine import get_session
from ingestion.crawler.config import CrawlConfig, CrawlMode
from ingestion.crawler.models import DiscoveryResult
from ingestion.pipeline_types import FileType
from ingestion.queue.document_producer import register_document_version

# Every route here requires an authenticated `member`. Declared on the router
# rather than per-endpoint so a route added later is protected by default --
# the failure mode of per-endpoint dependencies is the endpoint someone
# forgets, and it fails open.
router = APIRouter(
    prefix="/ingest",
    tags=["ingest"],
    dependencies=[Depends(require_member)],
)

DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000000")

# How long a discovery stays confirmable — long enough for a human to read
# the list and decide, short enough that a stale list is never acted on.
_DISCOVERY_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class _CachedDiscovery:
    result: DiscoveryResult
    config: CrawlConfig


# ---------------------------------------------------------------------------
# Discovery review state (P2-5)
#
# This was a module-level dict. Discovery and confirmation are two separate
# HTTP requests, so with more than one API instance the confirm had roughly a
# 50% chance of landing on a process that had never heard of the discovery —
# and the 404 it returned said "unknown or expired discovery_id", blaming a
# TTL that had not elapsed. A single instance restarting between the two
# calls produced the same misleading error.
#
# The rows now live in `crawl_discovery` (migration 3d6f8b2c17ae), where
# every instance can see them, with `expires_at` carrying the TTL that used
# to be a `time.monotonic()` delta. Expired rows are swept on the way past
# rather than by a scheduled job: the volume is a handful of rows and the
# sweep is one indexed DELETE.
# ---------------------------------------------------------------------------


def _serialize_discovery(result: DiscoveryResult) -> dict:
    return {
        "source": result.source,
        "pages": [
            {"url": p.url, "kind": p.kind, "file_type": p.file_type} for p in result.pages
        ],
    }


def _deserialize_discovery(data: dict) -> DiscoveryResult:
    from ingestion.crawler.models import DiscoveredPage

    return DiscoveryResult(
        pages=tuple(
            DiscoveredPage(url=p["url"], kind=p["kind"], file_type=p.get("file_type"))
            for p in data["pages"]
        ),
        source=data["source"],
    )


def _serialize_config(config: CrawlConfig) -> dict:
    data = asdict(config)
    # `mode` is a str-Enum and `allowed_domains` a tuple; both round-trip
    # through JSON as plain values, so they are normalised explicitly here
    # rather than relying on the JSON encoder's treatment of subclasses.
    data["mode"] = config.mode.value
    data["allowed_domains"] = list(config.allowed_domains)
    return data


def _deserialize_config(data: dict) -> CrawlConfig:
    # Tolerate rows written before a field was added or after one was
    # removed: a stale discovery is worth degrading to defaults for, not
    # worth a 500. Unknown keys are dropped, missing ones take their default.
    known = {f.name for f in fields(CrawlConfig)}
    kwargs = {k: v for k, v in data.items() if k in known}
    if "mode" in kwargs:
        kwargs["mode"] = CrawlMode(kwargs["mode"])
    if "allowed_domains" in kwargs:
        kwargs["allowed_domains"] = tuple(kwargs["allowed_domains"])
    return CrawlConfig(**kwargs)


async def _cache_discovery(
    session: AsyncSession, result: DiscoveryResult, config: CrawlConfig
) -> str:
    from db.models import CrawlDiscovery

    discovery_id = uuid.uuid4()
    session.add(
        CrawlDiscovery(
            discovery_id=discovery_id,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=_DISCOVERY_TTL_SECONDS),
            result=_serialize_discovery(result),
            config=_serialize_config(config),
        )
    )
    await session.flush()
    return discovery_id.hex


async def _get_discovery(session: AsyncSession, discovery_id: str) -> _CachedDiscovery | None:
    from db.models import CrawlDiscovery

    now = datetime.now(timezone.utc)
    await session.execute(delete(CrawlDiscovery).where(CrawlDiscovery.expires_at < now))

    try:
        key = UUID(discovery_id)
    except ValueError:
        # A malformed id is a 404 like any other unknown one — never a 500.
        return None

    row = await session.get(CrawlDiscovery, key)
    if row is None:
        return None
    return _CachedDiscovery(
        result=_deserialize_discovery(row.result),
        config=_deserialize_config(row.config),
    )


def _uploaded_by() -> UUID:
    """The fallback owner for ingestion that has no authenticated caller.

    Since P0-3 the API paths pass the signed-in user's id instead, so this is
    only reached by the worker and the offline scripts. It is kept because
    `knowledge_source.uploaded_by` is NOT NULL and a background job genuinely
    has no person behind it -- not as a default for HTTP requests, which is
    what it used to be, and which meant no upload was attributable to anyone.
    """
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

async def _assert_fetchable(url: str) -> None:
    """Reject a URL the server must not fetch, as a 400 rather than a 500."""
    try:
        await assert_url_is_safe(url)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


RouteKind = Literal["html", "document"]


def _classify_content_type(content_type: str) -> RouteKind:
    base = content_type.split(";", 1)[0].strip().lower()
    return "document" if base in _CRAWL_DOC_MIME_TO_FILE_TYPE else "html"


def _resolve_file_type(content_type: str) -> FileType | None:
    base = content_type.split(";", 1)[0].strip().lower()
    return _CRAWL_DOC_MIME_TO_FILE_TYPE.get(base)


async def _register_and_enqueue(
    session: AsyncSession,
    *,
    url: str,
    raw_bytes: bytes,
    mime_type: str,
    file_type: FileType | None,
    category_id: UUID | None,
    uploaded_by: UUID | None = None,
) -> dict:
    job = await register_document_version(
        session,
        url=url,
        raw_bytes=raw_bytes,
        mime_type=mime_type,
        file_type=file_type,
        uploaded_by=uploaded_by or _uploaded_by(),
        category_id=category_id,
    )
    if job is None:
        return {"status": "duplicate_skipped"}
    return {
        "status": "submitted",
        "job_id": str(job.job_id),
        "source_id": str(job.source_id),
        "version_id": str(job.version_id),
    }


@router.get("/jobs/{job_id}")
async def job_status(job_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    from db.models import KnowledgeInjectionJob

    row = await session.get(KnowledgeInjectionJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": str(row.job_id),
        "status": row.status,
        "chunks_created_count": row.chunks_created_count,
        "entities_created_count": row.entities_created_count,
        "error_details": row.error_details,
    }


@router.post("/upload", status_code=202)
async def upload(
    file: UploadFile,
    category_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(enforce_ingest_quota),
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
    return await _register_and_enqueue(
        session,
        url=f"manual_upload://{name}",
        raw_bytes=data,
        mime_type=mime,
        file_type=file_type,
        category_id=category_id,
        uploaded_by=principal.id,
    )


class WaitFields(BaseModel):
    # How long to wait for SPA/client-rendered pages to finish before
    # capturing HTML (see ingestion.crawler.config.CrawlConfig.wait_strategy).
    wait_strategy: Literal["fixed_timeout", "networkidle", "selector"] = "fixed_timeout"
    wait_selector: str | None = None  # required only when wait_strategy == "selector"

    @model_validator(mode="after")
    def _validate_wait(self):
        if self.wait_strategy == "selector" and not self.wait_selector:
            raise ValueError(
                "wait_selector is required when wait_strategy == 'selector'"
            )
        return self


class CrawlRequest(WaitFields):
    url: str = Field(..., min_length=5, max_length=2048)
    category_id: UUID | None = None
    # PAGE (default) crawls only the given URL — no discovery, no review step.
    # SITE returns a discovery list for review instead of crawling in the same
    # request; the caller then POSTs /crawl/{discovery_id}/confirm.
    scope: Literal["PAGE", "SITE"] = "PAGE"


class DiscoverRequest(WaitFields):
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


async def _run_discovery(
    root_url: str, *, wait_strategy: str, wait_selector: str | None
) -> tuple[DiscoveryResult, CrawlConfig]:
    from urllib.parse import urlsplit

    from ingestion.crawler.crawler import discover

    # The crawler's fetch boundary checks every URL it touches, so this is
    # not the only guard -- it is the one that answers a hostile root with a
    # 400 in milliseconds instead of after Chromium has started.
    await _assert_fetchable(root_url)

    host = urlsplit(root_url).netloc
    config = CrawlConfig(
        mode=CrawlMode.SITE,
        allowed_domains=(host,),
        wait_strategy=wait_strategy,
        wait_selector=wait_selector,
    )
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
    session: AsyncSession,
    documents,
    category_id: UUID | None,
    uploaded_by: UUID | None = None,
) -> dict:
    """Register + run ingestion for every successfully crawled item."""
    outcomes = []
    for doc in documents:
        if doc.error is not None:
            outcomes.append({"url": doc.url, "status": "failed", "error": doc.error})
            continue
        outcome = await _register_and_enqueue(
            session,
            url=doc.url,
            raw_bytes=doc.content if doc.file_type else doc.markdown.encode("utf-8"),
            mime_type=_crawl_mime(doc.file_type),
            file_type=FileType.MD if not doc.file_type else _crawl_file_type(doc.file_type),
            category_id=category_id,
            uploaded_by=uploaded_by,
        )
        outcome["url"] = doc.url
        outcomes.append(outcome)
    return {"status": "submitted", "results": outcomes}


async def _crawl_single_page(
    req: CrawlRequest, session: AsyncSession, uploaded_by: UUID | None = None
) -> dict:
    """PAGE scope: crawl exactly the given URL (v1 behavior), no discovery."""
    try:
        # `safe_get` rather than a bare client: it validates the URL, walks
        # redirects one hop at a time re-validating each, and re-checks the
        # address actually connected to. `follow_redirects=True` here was the
        # bypass -- a public URL that 302s to 169.254.169.254 sailed straight
        # through the check on the URL the caller supplied.
        response = await safe_get(req.url, timeout=30.0)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "application/octet-stream")
        raw_bytes = response.content
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}") from exc

    route = _classify_content_type(content_type)

    if route == "html":
        # The page body was already fetched above — extract markdown from it
        # directly instead of re-fetching through the Crawler (which used a
        # stricter user-agent/timeout and double-fetched the URL).
        from ingestion.crawler.exception import ExtractionError
        from ingestion.crawler.extractor import extract_markdown

        final_url = str(response.url)
        html = raw_bytes.decode("utf-8", errors="replace")
        try:
            markdown = extract_markdown(html, final_url)
        except ExtractionError as exc:
            raise HTTPException(status_code=502, detail=f"Crawl failed: {exc}") from exc
        return await _register_and_enqueue(
            session,
            url=final_url,
            raw_bytes=markdown.encode("utf-8"),
            mime_type="text/markdown",
            file_type=FileType.MD,
            category_id=req.category_id,
            uploaded_by=uploaded_by,
        )

    from urllib.parse import urlsplit

    from ingestion.crawler.crawler import crawl_confirmed
    from ingestion.crawler.documents import classify_url

    host = urlsplit(req.url).netloc
    config = CrawlConfig(
        mode=CrawlMode.SITE,
        allowed_domains=(host,),
        wait_strategy=req.wait_strategy,
        wait_selector=req.wait_selector,
    )
    page = classify_url(req.url)
    documents = await crawl_confirmed((page,), config)
    return await _ingest_documents(session, documents, req.category_id, uploaded_by)


@router.post("/crawl", status_code=202)
async def crawl(
    req: CrawlRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(enforce_ingest_quota),
) -> dict:
    if req.scope == "PAGE":
        return await _crawl_single_page(req, session, principal.id)

    # SITE scope: discovery-first, always goes through a review step before any
    # crawling happens. No same-request auto-crawl.
    result, config = await _run_discovery(
        req.url, wait_strategy=req.wait_strategy, wait_selector=req.wait_selector
    )
    discovery_id = await _cache_discovery(session, result, config)
    return _review_payload(discovery_id, result)


@router.post("/crawl/discover")
async def crawl_discover(
    req: DiscoverRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(enforce_ingest_quota),
) -> dict:
    result, config = await _run_discovery(
        req.root_url, wait_strategy=req.wait_strategy, wait_selector=req.wait_selector
    )
    discovery_id = await _cache_discovery(session, result, config)
    return _review_payload(discovery_id, result)


@router.post("/crawl/{discovery_id}/confirm", status_code=202)
async def crawl_confirm(
    discovery_id: str,
    req: ConfirmRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(enforce_ingest_quota),
) -> dict:
    from ingestion.crawler.crawler import crawl_confirmed

    cached = await _get_discovery(session, discovery_id)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired discovery_id. Run /crawl/discover again.",
        )
    documents = await crawl_confirmed(cached.result.pages, cached.config)
    return await _ingest_documents(
        session, documents, req.category_id, principal.id
    )
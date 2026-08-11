"""HTTP ingestion endpoints.

Both paths reuse the exact orchestration the CLI worker uses
(``scripts/crawl_and_ingest.py``): register a document version (checksum
dedup, MinIO storage, queue row) then run ``run_ingestion`` immediately so
the result is indexable without a separate worker process.

  POST /ingest/upload   multipart file (PDF / DOCX / Markdown)
  POST /ingest/crawl    JSON {"url": ...} (HTML page or PDF/Office URL)

Uploaded-by defaults to the system service account
(``00000000-0000-0000-0000-000000000000``); override with
``INGEST_DEFAULT_USER_ID`` in the environment.
"""

from __future__ import annotations

import os
import uuid
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.async_session import get_session, session_factory
from ingestion.pipeline_types import FileType, JobType, JobRef, JobStatus
from ingestion.queue.document_producer import register_document_version

router = APIRouter(prefix="/ingest", tags=["ingest"])

DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000000")


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


@router.post("/crawl")
async def crawl(
    req: CrawlRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(req.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")
            raw_bytes = response.content
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}") from exc

    route = _classify_content_type(content_type)

    if route == "html":
        from ingestion.crawler.config import CrawlConfig, CrawlMode
        from ingestion.crawler.crawler import Crawler

        documents = await Crawler(CrawlConfig(mode=CrawlMode.PAGE)).crawl(req.url)
        doc = documents[0] if documents else None
        if doc is None or doc.error is not None:
            detail = f"Crawl failed: {doc.error if doc else 'no document returned'}"
            raise HTTPException(status_code=502, detail=detail)
        return await _register_and_run(
            session,
            url=doc.url,
            raw_bytes=doc.markdown.encode("utf-8"),
            mime_type="text/markdown",
            file_type=FileType.MD,
            category_id=req.category_id,
        )

    return await _register_and_run(
        session,
        url=req.url,
        raw_bytes=raw_bytes,
        mime_type=content_type,
        file_type=_resolve_file_type(content_type),
        category_id=req.category_id,
    )
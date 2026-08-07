"""
Producer-side helper for the ONE case the existing crawler pipeline doesn't
already cover: a URL that points straight at a document (PDF/DOCX/PPTX/...)
rather than an HTML page.

Per the README, the crawler already stores HTML pages to MinIO and creates
a queued knowledge_injection_job (that path is reused as-is in
scripts/crawl_and_ingest.py -- see the ASSUMPTIONS note there). This module
exists only to do the same two things (store bytes, queue a job) for raw
document bytes fetched directly, matching ingestion_flow.md step 1-2, using
the real ORM models.

New file -- does not modify db/models.py, storage/*, or crawler/*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    KnowledgeInjectionJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from ingestion.dedup import (
    DuplicateContent,
    NewDocument,
    NewVersion,
    classify_upload,
    compute_checksum,
)
from ingestion.storage.client import StorageClient, StorageConfig
from ingestion.storage.config import ORIGIN_WEB_CRAWL
from ingestion.storage.keys import build_key
from ingestion.pipeline_types import (
    FileType,
    JobRef,
    JobStatus,
    JobType,
    SourceType,
)


async def _find_checksum_match(session: AsyncSession, checksum: str) -> UUID | None:
    row = (
        await session.execute(
            select(KnowledgeSourceVersion.version_id).where(
                KnowledgeSourceVersion.checksum == checksum,
                # only a version that actually finished counts as a real
                # duplicate -- a PENDING/FAILED row is unfinished work, not
                # proof this content was ever successfully ingested
                KnowledgeSourceVersion.status.in_(("INDEXED", "STALE")),
            )
        )
    ).scalar_one_or_none()
    return row

async def _find_retryable_version(session: AsyncSession, checksum: str) -> UUID | None:
    """A version with this checksum that never finished successfully --
    safe to retry in place. Prevents the checksum UNIQUE constraint from
    rejecting a retry of identical content as if it were a second document."""
    return (
        await session.execute(
            select(KnowledgeSourceVersion.version_id).where(
                KnowledgeSourceVersion.checksum == checksum,
                KnowledgeSourceVersion.status.notin_(("INDEXED", "STALE")),
            )
        )
    ).scalar_one_or_none()


def _build_storage_client() -> StorageClient:
    return StorageClient(
        StorageConfig(
            endpoint=os.environ["MINIO_ENDPOINT"],
            access_key=os.environ["MINIO_ROOT_USER"],
            secret_key=os.environ["MINIO_ROOT_PASSWORD"],
            bucket_name=os.environ.get("MINIO_BUCKET", "knowledge-documents"),
            secure=(os.environ.get("MINIO_SECURE", "true") or "true").strip().lower()
            in {"1", "true", "yes", "on"},
        )
    )


async def _requeue_existing_version(
    session: AsyncSession, version_id: UUID, *, raw_bytes: bytes, mime_type: str
) -> KnowledgeInjectionJob:
    """A version with this checksum exists but never finished -- rather
    than trust whatever storage_uri was recorded last time (it may be
    stale, e.g. from before build_key's format changed, or the original
    upload may be the thing that failed), rebuild the key fresh and
    re-upload every time this path runs. Only fires on retries, so the
    extra I/O is cheap relative to correctness."""
    version = await session.get(KnowledgeSourceVersion, version_id)

    storage_key = build_key(ORIGIN_WEB_CRAWL, version.source_id, version.version_id)
    storage_client = _build_storage_client()
    storage_client.put_object(storage_key, raw_bytes, mime_type or "application/octet-stream")

    version.storage_uri = storage_key
    version.status = "PENDING"

    job = KnowledgeInjectionJob(
        job_id=uuid4(),
        source_id=version.source_id,
        version_id=version.version_id,
        job_type=("REINDEX" if version.version_number > 1 else "INITIAL_INGEST"),
        status="QUEUED",
        triggered_by="system:crawler",
    )
    session.add(job)
    await session.flush()
    await session.commit()
    return job

async def _find_source_by_external_id(session: AsyncSession, external_reference_id: str) -> KnowledgeSource | None:
    return (
        await session.execute(
            select(KnowledgeSource).where(KnowledgeSource.external_reference_id == external_reference_id)
        )
    ).scalar_one_or_none()


async def _latest_version_number(session: AsyncSession, source_id: UUID) -> int:
    from sqlalchemy import func

    return (
        await session.execute(
            select(func.max(KnowledgeSourceVersion.version_number)).where(
                KnowledgeSourceVersion.source_id == source_id
            )
        )
    ).scalar_one()


@dataclass(frozen=True, slots=True)
class DocumentVersionPlan:
    """What register_document_version needs to create, decided purely from
    the dedup classification -- no I/O. Separated out specifically so the
    match-statement dispatch (source of a real bug: `case type() if ...`
    is not how you match an instance's class) is unit-testable without a
    database. See tests/ingestion/test_document_producer.py."""

    new_source: KnowledgeSource | None
    source_id: UUID
    version_number: int


def _plan_version(
    classification: NewDocument | NewVersion, *, url: str, category_id: UUID | None, uploaded_by: UUID
) -> DocumentVersionPlan:
    """Pure: given a non-duplicate classification, decide whether a new
    KnowledgeSource row is needed and what version_number to use. Does not
    touch the session -- the caller adds `new_source` if it's not None."""
    match classification:
        case NewDocument():
            source = KnowledgeSource(
                source_id=uuid4(),
                category_id=category_id,
                source_name=url.rsplit("/", 1)[-1] or url,
                source_type=SourceType.EXTERNAL_INTEGRATION.value,
                origin_system="crawler",
                external_reference_id=url,
                uploaded_by=uploaded_by,
                current_version_id=None,
                is_active=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            return DocumentVersionPlan(new_source=source, source_id=source.source_id, version_number=1)

        case NewVersion(source_id=existing_id, next_version_number=next_number):
            return DocumentVersionPlan(new_source=None, source_id=existing_id, version_number=next_number)


async def register_document_version(
    session: AsyncSession,
    *,
    url: str,
    raw_bytes: bytes,
    mime_type: str,
    file_type: FileType | None,
    uploaded_by: UUID,
    category_id: UUID | None,
) -> KnowledgeInjectionJob | None:
    """
    ingestion_flow.md step 1 + 2 for a directly-fetched document URL.

    Returns the queued job's ORM row, or None if this exact content was
    already ingested (global checksum dedup -> skip entirely).
    """
    checksum = compute_checksum(raw_bytes)

    retryable_version_id = await _find_retryable_version(session, checksum)
    if retryable_version_id is not None:
        return await _requeue_existing_version(
            session, retryable_version_id, raw_bytes=raw_bytes, mime_type=mime_type
        )

    checksum_match = await _find_checksum_match(session, checksum)

    existing_source = await _find_source_by_external_id(session, url)
    latest_version_number = (
        await _latest_version_number(session, existing_source.source_id) if existing_source else None
    )

    classification = classify_upload(
        checksum_match=checksum_match,
        existing_source_id=existing_source.source_id if existing_source else None,
        latest_version_number=latest_version_number,
    )

    if isinstance(classification, DuplicateContent):
        import logging

        logging.getLogger(__name__).info(
            "skipping %s: identical content already ingested as version %s",
            url,
            classification.existing_version_id,
        )
        return None  # skip ingestion entirely, per step 1

    plan = _plan_version(classification, url=url, category_id=category_id, uploaded_by=uploaded_by)
    if plan.new_source is not None:
        session.add(plan.new_source)
    source_id, version_number = plan.source_id, plan.version_number

    version = KnowledgeSourceVersion(
        version_id=uuid4(),
        source_id=source_id,
        version_number=version_number,
        file_type=file_type.value if file_type else None,
        storage_uri="",  # set below after the MinIO key is built
        checksum=checksum,
        mime_type=mime_type,
        file_size_bytes=len(raw_bytes),
        status="PENDING",
        metadata_={"origin_url": url},
        created_at=datetime.now(UTC),
    )
    storage_key = build_key(ORIGIN_WEB_CRAWL, source_id, version.version_id)
    version.storage_uri = storage_key

    storage_client = StorageClient(
        StorageConfig(
            endpoint=os.environ["MINIO_ENDPOINT"],
            access_key=os.environ["MINIO_ROOT_USER"],
            secret_key=os.environ["MINIO_ROOT_PASSWORD"],
            bucket_name=os.environ.get("MINIO_BUCKET", "knowledge-documents"),
            secure=(os.environ.get("MINIO_SECURE", "true") or "true").strip().lower()
            in {"1", "true", "yes", "on"},
        )
    )
    try:
        storage_client.put_object(storage_key, raw_bytes, mime_type or "application/octet-stream")
    except Exception:
        import logging

        logging.getLogger(__name__).exception("MinIO upload failed for %s", url)
        raise

    session.add(version)

    job = KnowledgeInjectionJob(
        job_id=uuid4(),
        source_id=source_id,
        version_id=version.version_id,
        job_type=(JobType.INITIAL_INGEST if version_number == 1 else JobType.REINDEX).value,
        status=JobStatus.QUEUED.value,
        triggered_by="system:crawler",
    )
    session.add(job)

    await session.flush()
    await session.commit()
    return job

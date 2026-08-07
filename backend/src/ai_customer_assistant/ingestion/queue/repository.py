"""
I/O layer for treating `knowledge_injection_job` as a queue table.

UPDATED: rewritten against the real ORM models in db.models
and AsyncSession, matching the style of storage/repository.py, instead of the
raw text() SQL used in the first draft.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from db.models import (
    EmbeddingChunk,
    KnowledgeInjectionJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from ingestion.pipeline_types import (
    FileType,
    JobRef,
    JobStatus,
    JobType,
    SourceRef,
    SourceType,
    VersionRef,
    VersionStatus,
)


async def claim_next_job(session: AsyncSession) -> JobRef | None:
    """
    Atomically claim one eligible job, honoring the one-job-per-source
    guard from ingestion_flow.md step 2 (option b): skip any source that
    already has a RUNNING job. Commits immediately so the RUNNING status
    (and the SKIP LOCKED release) is visible to other workers right away,
    rather than holding the row lock for the whole job duration.
    """
    running = aliased(KnowledgeInjectionJob)
    guard = (
        select(running.job_id)
        .where(running.source_id == KnowledgeInjectionJob.source_id, running.status == "RUNNING")
        .exists()
    )
    stmt = (
        select(KnowledgeInjectionJob)
        .where(KnowledgeInjectionJob.status == "QUEUED", ~guard)
        .order_by(KnowledgeInjectionJob.started_at.nulls_first(), KnowledgeInjectionJob.job_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None

    row.status = "RUNNING"
    row.started_at = datetime.now(UTC)
    await session.flush()
    await session.commit()

    return JobRef(
        job_id=row.job_id,
        source_id=row.source_id,
        version_id=row.version_id,
        job_type=JobType(row.job_type),
        status=JobStatus(row.status),
        triggered_by=row.triggered_by,
    )


async def load_source(session: AsyncSession, source_id: UUID) -> SourceRef:
    row = await session.get(KnowledgeSource, source_id)
    return SourceRef(
        source_id=row.source_id,
        category_id=row.category_id,
        source_name=row.source_name or "",
        source_type=SourceType(row.source_type),
        origin_system=row.origin_system,
        external_reference_id=row.external_reference_id,
        uploaded_by=row.uploaded_by,
        current_version_id=row.current_version_id,
    )


async def load_version(session: AsyncSession, version_id: UUID) -> VersionRef:
    row = await session.get(KnowledgeSourceVersion, version_id)
    return VersionRef(
        version_id=row.version_id,
        source_id=row.source_id,
        version_number=row.version_number,
        file_type=FileType(row.file_type) if row.file_type else None,
        storage_uri=row.storage_uri,
        checksum=row.checksum,
        mime_type=row.mime_type or "",
        file_size_bytes=row.file_size_bytes or 0,
        status=VersionStatus(row.status),
        metadata=row.metadata_ or {},  # note: ORM attribute is metadata_, db column is "metadata"
    )


async def previous_version_chunk_checksums(
    session: AsyncSession, source_id: UUID, before_version_number: int
) -> dict[str, tuple[float, ...]]:
    """
    Checksum -> embedding for every chunk of the version immediately
    preceding `before_version_number`. Returning the embedding (not just
    the checksum) lets the pipeline actually copy the vector forward for
    a match, per ingestion_flow.md step 4 -- chunk_embed.process_document
    has no reuse-by-checksum concept, so the pipeline applies it as a
    post-processing swap using this map.
    """
    prev_number = (
        await session.execute(
            select(func.max(KnowledgeSourceVersion.version_number)).where(
                KnowledgeSourceVersion.source_id == source_id,
                KnowledgeSourceVersion.version_number < before_version_number,
            )
        )
    ).scalar_one_or_none()
    if prev_number is None:
        return {}

    rows = (
        await session.execute(
            select(EmbeddingChunk.checksum, EmbeddingChunk.embedding)
            .join(KnowledgeSourceVersion, KnowledgeSourceVersion.version_id == EmbeddingChunk.version_id)
            .where(
                KnowledgeSourceVersion.source_id == source_id,
                KnowledgeSourceVersion.version_number == prev_number,
            )
        )
    ).all()
    return {row.checksum: tuple(row.embedding) for row in rows}


async def mark_version_status(session: AsyncSession, version_id: UUID, status: VersionStatus) -> None:
    version = await session.get(KnowledgeSourceVersion, version_id)
    version.status = status.value
    await session.flush()
    await session.commit()


async def cutover(
    session: AsyncSession, *, source_id: UUID, new_version_id: UUID, old_version_id: UUID | None
) -> None:
    """Step 7a as a single transaction: INDEXED + current_version_id + updated_at,
    then STALE the old version."""
    new_version = await session.get(KnowledgeSourceVersion, new_version_id)
    new_version.status = "INDEXED"

    source = await session.get(KnowledgeSource, source_id)
    source.current_version_id = new_version_id
    source.updated_at = datetime.now(UTC)

    if old_version_id is not None:
        old_version = await session.get(KnowledgeSourceVersion, old_version_id)
        old_version.status = "STALE"

    await session.flush()
    await session.commit()


async def complete_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    status: JobStatus,
    chunks_created_count: int,
    entities_created_count: int,
    error_details: str | None,
) -> None:
    job = await session.get(KnowledgeInjectionJob, job_id)
    job.status = status.value
    job.completed_at = datetime.now(UTC)
    job.chunks_created_count = chunks_created_count
    job.entities_created_count = entities_created_count
    job.error_details = error_details
    await session.flush()
    await session.commit()

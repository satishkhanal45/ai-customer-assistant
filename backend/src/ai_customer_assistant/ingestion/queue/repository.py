"""
I/O layer for treating `knowledge_injection_job` as a queue table.

UPDATED: rewritten against the real ORM models in db.models
and AsyncSession, matching the style of storage/repository.py, instead of the
raw text() SQL used in the first draft.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from db.models import (
    EmbeddingChunk,
    KnowledgeInjectionJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from ingestion.queue import retry
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
    # A job waiting out its backoff is QUEUED but not yet *eligible*. Without
    # this clause a retry would be claimed on the very next poll, which is a
    # busy-wait against whatever was rate-limiting us in the first place.
    ready = or_(
        KnowledgeInjectionJob.next_attempt_at.is_(None),
        KnowledgeInjectionJob.next_attempt_at <= datetime.now(UTC).replace(tzinfo=None),
    )
    stmt = (
        select(KnowledgeInjectionJob)
        .where(KnowledgeInjectionJob.status == "QUEUED", ready, ~guard)
        .order_by(KnowledgeInjectionJob.started_at.nulls_first(), KnowledgeInjectionJob.job_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None

    row.status = "RUNNING"
    row.started_at = datetime.now(UTC)
    # The backoff has been served. Keeping the column meaningful -- set only
    # while a job is actually waiting -- is what lets "is this job held back?"
    # stay a single-column question.
    row.next_attempt_at = None
    await session.flush()
    await session.commit()

    return JobRef(
        job_id=row.job_id,
        source_id=row.source_id,
        version_id=row.version_id,
        job_type=JobType(row.job_type),
        status=JobStatus(row.status),
        triggered_by=row.triggered_by,
        attempt_count=row.attempt_count or 0,
    )


async def reset_stale_running_jobs(session: AsyncSession, *, older_than_seconds: float) -> int:
    """Return jobs stuck in RUNNING back to QUEUED, and report how many.

    A worker that is killed mid-job (deploy, OOM, `docker compose down`)
    leaves its row RUNNING forever. That is not merely untidy: `claim_next_job`
    skips any source that has a RUNNING job, so one abandoned row blocks every
    future ingestion for that source permanently, with no error anywhere.

    Called at worker startup. `older_than_seconds` must comfortably exceed the
    longest legitimate job, or this will yank a job out from under a healthy
    worker that is still processing it.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    stale = (
        await session.execute(
            select(KnowledgeInjectionJob).where(
                KnowledgeInjectionJob.status == "RUNNING",
                KnowledgeInjectionJob.started_at < cutoff,
            )
        )
    ).scalars().all()

    for job in stale:
        # An abandoned job has consumed an attempt. Without counting it, a
        # document that kills the worker every time -- an OOM on a huge PDF,
        # say -- is requeued forever, and because the reaper runs at startup
        # it gets a fresh worker to kill each time. Counting it means such a
        # job reaches DEAD_LETTER like any other repeat offender.
        decision = retry.decide(
            failure_kind=retry.WORKER_ABANDONED,
            attempt_count=job.attempt_count or 0,
        )
        job.status = decision.status
        job.started_at = None
        job.attempt_count = decision.attempt_count
        job.next_attempt_at = _naive(decision.next_attempt_at)
        job.failure_kind = retry.WORKER_ABANDONED
        job.error_details = "requeued: worker did not finish this job"
        if not decision.will_retry:
            job.completed_at = datetime.now(UTC).replace(tzinfo=None)
            job.error_details = (
                f"worker did not finish this job after "
                f"{decision.attempt_count} attempt(s)"
            )

    if stale:
        await session.flush()
        await session.commit()
    return len(stale)


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


def _naive(moment: datetime | None) -> datetime | None:
    """Drop the tzinfo for columns declared without timezone.

    `started_at` / `completed_at` / `next_attempt_at` are all naive DateTime,
    and SQLite rejects an aware value outright rather than coercing it.
    """
    return moment.replace(tzinfo=None) if moment is not None else None


async def complete_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    status: JobStatus,
    chunks_created_count: int,
    entities_created_count: int,
    error_details: str | None,
    failure_kind: str | None = None,
) -> JobStatus:
    """Record a finished job, applying the retry policy to a failed one.

    Returns the status actually written, which is not always the one passed
    in: a FAILED outcome whose failure kind is transient and still has budget
    is written back as QUEUED with a `next_attempt_at`, and one that has run
    out of budget becomes DEAD_LETTER. Success is recorded as-is.

    The policy lives in `retry.decide`; this function is the write.
    """
    job = await session.get(KnowledgeInjectionJob, job_id)
    job.chunks_created_count = chunks_created_count
    job.entities_created_count = entities_created_count
    job.error_details = error_details
    job.failure_kind = failure_kind

    if status is not JobStatus.FAILED:
        job.status = status.value
        job.completed_at = datetime.now(UTC).replace(tzinfo=None)
        job.next_attempt_at = None
        await session.flush()
        await session.commit()
        return status

    decision = retry.decide(
        failure_kind=failure_kind, attempt_count=job.attempt_count or 0
    )
    job.status = decision.status
    job.attempt_count = decision.attempt_count
    job.next_attempt_at = _naive(decision.next_attempt_at)
    # A job going back on the queue has not completed. Leaving a stale
    # `completed_at` on it would make the Jobs page sort a pending retry in
    # among the finished work.
    job.completed_at = (
        None if decision.will_retry else datetime.now(UTC).replace(tzinfo=None)
    )
    await session.flush()
    await session.commit()
    return JobStatus(decision.status)

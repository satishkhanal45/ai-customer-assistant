"""
The consumer side of the PGQueue: polls knowledge_injection_job, claims one
eligible job at a time, and dispatches it to the ingestion pipeline.

UPDATED: async throughout, using an AsyncSession session_factory
(sqlalchemy.ext.asyncio.async_sessionmaker), matching storage/repository.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ingestion.pipeline import run_delete, run_ingestion
from ingestion.pipeline_types import JobOutcome, JobRef, JobStatus, JobType
from ingestion.queue import repository
from ingestion.queue.config import PGQueueSettings

logger = logging.getLogger(__name__)

JobHandler = Callable[[AsyncSession, JobRef], Awaitable[JobOutcome]]

# job_type -> handler. Replaces an if/elif over job.job_type.
_HANDLERS: dict[JobType, JobHandler] = {
    JobType.INITIAL_INGEST: run_ingestion,
    JobType.REINDEX: run_ingestion,
    JobType.UPDATE: run_ingestion,
    JobType.DELETE: run_delete,
}


@dataclass(frozen=True, slots=True)
class WorkerDeps:
    """Everything the worker loop needs, injected rather than imported as
    module-level globals -- keeps the loop testable without a real DB."""

    session_factory: async_sessionmaker
    settings: PGQueueSettings
    handlers: dict[JobType, JobHandler] | None = None

    def resolved_handlers(self) -> dict[JobType, JobHandler]:
        return self.handlers or _HANDLERS


async def _process_one(session: AsyncSession, job: JobRef, handlers: dict[JobType, JobHandler]) -> JobOutcome:
    handler = handlers[job.job_type]
    return await handler(session, job)


async def _record_outcome(session: AsyncSession, outcome: JobOutcome) -> JobOutcome:
    await repository.complete_job(
        session,
        job_id=outcome.job_id,
        status=outcome.status,
        chunks_created_count=outcome.chunks_created_count,
        entities_created_count=outcome.entities_created_count,
        error_details=outcome.error_details,
    )
    return outcome


async def poll_once(deps: WorkerDeps) -> JobOutcome | None:
    """Claim and fully process at most one job. Returns None if the queue
    was empty this tick."""
    async with deps.session_factory() as session:
        job = await repository.claim_next_job(session)
        if job is None:
            return None

        logger.info("claimed job %s (%s) for source %s", job.job_id, job.job_type, job.source_id)
        try:
            outcome = await _process_one(session, job, deps.resolved_handlers())
        except Exception as exc:  # noqa: BLE001 -- last-resort catch so a crash
            # never leaves a job stuck RUNNING forever (blocks the
            # one-job-per-source guard permanently otherwise)
            logger.exception("job %s crashed unexpectedly", job.job_id)
            outcome = JobOutcome(
                job_id=job.job_id,
                version_id=job.version_id,
                status=JobStatus.FAILED,
                error_details=f"unhandled_exception: {exc}",
            )
        return await _record_outcome(session, outcome)


async def run_worker(deps: WorkerDeps, *, max_iterations: int | None = None) -> None:
    """
    The long-running consumer entry point (see scripts/run_worker.py).
    `max_iterations` exists purely so tests can bound the loop; production
    callers leave it None and run until the process is stopped.
    """
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        outcome = await poll_once(deps)
        if outcome is None:
            await asyncio.sleep(deps.settings.poll_interval_seconds)
        iterations += 1

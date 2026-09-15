"""
The consumer side of the PGQueue: polls knowledge_injection_job, claims one
eligible job at a time, and dispatches it to the ingestion pipeline.

UPDATED: async throughout, using an AsyncSession session_factory
(sqlalchemy.ext.asyncio.async_sessionmaker), matching storage/repository.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ingestion.pipeline import run_delete, run_ingestion
from ingestion.pipeline_types import JobOutcome, JobRef, JobStatus, JobType
from ingestion.queue import repository
from ingestion.queue.config import PGQueueSettings
from logging_config import trace_context

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
    # Claims are serialised across this worker's lanes; the work they hand
    # back is not.
    #
    # `FOR UPDATE SKIP LOCKED` stops two lanes taking the same *row*, but the
    # one-job-per-source guard is a different question and that lock does not
    # cover it. The guard asks "does this source already have a RUNNING job?",
    # and a lane's RUNNING transition is invisible to the others until it
    # commits -- so two lanes selecting at the same instant can each pick a
    # *different* job for the *same* source and both proceed. That is exactly
    # what the guard exists to prevent: two runs writing chunks for one
    # document.
    #
    # Claiming is fast, so serialising it costs nothing measurable and closes
    # the window entirely. It lives on the deps rather than at module scope
    # because an `asyncio.Lock` binds to the loop that first acquires it, and
    # a process-global one would outlive the loop it was bound to.
    #
    # This covers lanes in one process, which is the concurrency this worker
    # offers. Two worker *processes* would still share that window --
    # pre-existing, and `PGQueueSettings.visibility_lock_id_namespace` is the
    # hook for closing it with a Postgres advisory lock if a deployment ever
    # runs more than one.
    claim_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # A job still RUNNING after this long belongs to a worker that died.
    # Must exceed the longest legitimate job or a healthy worker's job gets
    # requeued underneath it. 30 minutes is generous for a single document.
    stale_job_seconds: float = 1800.0

    def resolved_handlers(self) -> dict[JobType, JobHandler]:
        return self.handlers or _HANDLERS


async def _process_one(session: AsyncSession, job: JobRef, handlers: dict[JobType, JobHandler]) -> JobOutcome:
    handler = handlers[job.job_type]
    return await handler(session, job)


async def _record_outcome(session: AsyncSession, outcome: JobOutcome) -> JobOutcome:
    """Write the outcome, and say out loud what the retry policy decided.

    `complete_job` returns the status actually written, which for a failure is
    not the one handed in: a transient failure with budget left goes back on
    the queue instead. Logging the difference is what makes a retry visible --
    otherwise a document that quietly succeeds on its third attempt looks
    identical to one that succeeded first time, and a job that gave up looks
    like any other failure.
    """
    written = await repository.complete_job(
        session,
        job_id=outcome.job_id,
        status=outcome.status,
        chunks_created_count=outcome.chunks_created_count,
        entities_created_count=outcome.entities_created_count,
        error_details=outcome.error_details,
        failure_kind=outcome.failure_kind,
    )

    if written is JobStatus.QUEUED:
        logger.warning(
            "job %s failed (%s) and was requeued for another attempt",
            outcome.job_id,
            outcome.failure_kind,
        )
    elif written is JobStatus.DEAD_LETTER:
        logger.error(
            "job %s dead-lettered after exhausting its attempts (%s)",
            outcome.job_id,
            outcome.failure_kind,
        )
    elif written is JobStatus.FAILED:
        logger.error(
            "job %s failed terminally (%s)", outcome.job_id, outcome.failure_kind
        )

    return replace(outcome, status=written)


def _trace_id(job: JobRef) -> str:
    """A label for this *run* of the job, not for the job.

    `job_id` alone is no longer enough to find one run in the log: a job can
    be retried up to four times, and the worker may run several documents at
    once, so one id can name four interleaved runs. Suffixing the attempt
    number separates them, and keeping the job's own prefix means a single
    grep still finds all of them.
    """
    return f"{job.job_id.hex[:8]}.{job.attempt_count + 1}"


async def poll_once(deps: WorkerDeps) -> JobOutcome | None:
    """Claim and fully process at most one job. Returns None if the queue
    was empty this tick."""
    async with deps.session_factory() as session:
        async with deps.claim_lock:
            job = await repository.claim_next_job(session)
        if job is None:
            return None

        with trace_context(_trace_id(job)):
            return await _run_claimed(deps, session, job)


async def _run_claimed(
    deps: WorkerDeps, session: AsyncSession, job: JobRef
) -> JobOutcome:
    """Process one already-claimed job and record what happened.

    Split out of `poll_once` so the claim can happen outside the trace context
    and the work inside it: the id is derived from the claimed row, so there
    is nothing to label until the claim returns.
    """
    logger.info(
        "claimed job %s (%s) for source %s, attempt %d",
        job.job_id,
        job.job_type,
        job.source_id,
        job.attempt_count + 1,
    )
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
            # Terminal on purpose: an unhandled exception is a defect, and
            # retrying it three more times turns one stack trace into four
            # while changing nothing.
            failure_kind="unhandled_exception",
        )
    return await _record_outcome(session, outcome)


async def reap_stale_jobs(deps: WorkerDeps) -> int:
    """Requeue jobs abandoned by a previous worker. Returns the count.

    Runs once at startup. Without it a worker killed mid-job leaves a row
    RUNNING forever, and `claim_next_job`'s one-job-per-source guard then
    blocks that source's ingestion permanently.
    """
    async with deps.session_factory() as session:
        requeued = await repository.reset_stale_running_jobs(
            session, older_than_seconds=deps.stale_job_seconds
        )
    if requeued:
        logger.warning("requeued %d job(s) abandoned by a previous worker", requeued)
    return requeued


async def _lane(deps: WorkerDeps, max_iterations: int | None) -> None:
    """One claim-process-record loop. `max_iterations` bounds it for tests."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        outcome = await poll_once(deps)
        if outcome is None:
            await asyncio.sleep(deps.settings.poll_interval_seconds)
        iterations += 1


async def run_worker(deps: WorkerDeps, *, max_iterations: int | None = None) -> None:
    """
    The long-running consumer entry point (see scripts/run_worker.py).
    `max_iterations` exists purely so tests can bound the loop; production
    callers leave it None and run until the process is stopped. It bounds each
    lane, so with one lane it means exactly what it always did.

    Concurrency is several lanes in **one** process, not several processes.
    That distinction is the whole reason this was affordable: a second process
    would load its own 400 MB copy of the embedding model, and memory was the
    stated blocker. Lanes share one copy.

    What the concurrency actually buys is honest to state: extraction is
    network-bound on a provider with a daily token ceiling, so two lanes do
    not double throughput against that ceiling. What they do is stop one slow
    document holding the queue head -- while one lane waits on the model,
    another fetches, runs Tika and embeds -- and keep the queue moving when a
    document fails early.

    Claiming was already safe for this: `FOR UPDATE SKIP LOCKED` hands each
    lane a different row, and the one-job-per-source guard stops two lanes
    taking two jobs for the same document. Each lane opens its own session,
    which is required rather than tidy -- an AsyncSession must not be used
    from two tasks at once.
    """
    await reap_stale_jobs(deps)

    lanes = max(1, deps.settings.concurrency)
    if lanes == 1:
        await _lane(deps, max_iterations)
        return

    logger.info("running %d concurrent ingestion lanes", lanes)
    await asyncio.gather(*(_lane(deps, max_iterations) for _ in range(lanes)))

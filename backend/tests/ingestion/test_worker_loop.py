"""The worker loop: claim, dispatch, record (ingestion.md item 15).

`poll_once` is the seam where a claimed job, a handler and the retry policy
meet, and it had no test at all — the adjacent files covered the reaper
(`test_stale_job_reaper.py`) and the pipeline's rollback
(`test_partial_commit_rollback.py`) but not the thing that calls them. That
gap matters more since the retry work: `poll_once` can now write four
different statuses, and which one it writes is the difference between a
document that recovers by itself and one that stops forever.

Backed by real SQLite rows and fake handlers, so the claim query and the
status writes are exercised while the pipeline itself — Tika, MinIO, a
provider — stays out of it.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import KnowledgeInjectionJob
from ingestion.pipeline_types import JobOutcome, JobRef, JobStatus, JobType
from ingestion.queue import retry
from ingestion.queue.config import PGQueueSettings
from ingestion.queue.worker import (
    WorkerDeps,
    poll_once,
    reap_stale_jobs,
    run_worker,
)


@pytest.fixture
async def session_factory():
    """Only `knowledge_injection_job` is created: its siblings carry JSONB and
    pgvector columns SQLite cannot render, and SQLite does not enforce the
    foreign keys, so job rows insert fine without their parents."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(KnowledgeInjectionJob.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _deps(session_factory, handler, *, concurrency=1, poll_interval=0.0, **kw):
    """One lane by default.

    Pinned rather than left to the setting's default: these tests are about
    what a single loop does, and inheriting a default of 2 would make
    `max_iterations` mean twice as much work and turn several of them into
    races that pass by luck. Concurrency has its own tests below.
    """
    return WorkerDeps(
        session_factory=session_factory,
        settings=PGQueueSettings(
            poll_interval_seconds=poll_interval, concurrency=concurrency
        ),
        handlers={jt: handler for jt in JobType},
        **kw,
    )


async def _add_job(factory, **overrides) -> uuid.UUID:
    job_id = uuid.uuid4()
    fields = dict(
        job_id=job_id,
        source_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        job_type="INITIAL_INGEST",
        status="QUEUED",
        triggered_by="test",
        attempt_count=0,
    )
    fields.update(overrides)
    async with factory() as session:
        session.add(KnowledgeInjectionJob(**fields))
        await session.commit()
    return job_id


async def _row(factory, job_id):
    async with factory() as session:
        return await session.get(KnowledgeInjectionJob, job_id)


def _succeeds(chunks=3, entities=2):
    async def handler(session, job: JobRef) -> JobOutcome:
        return JobOutcome(
            job_id=job.job_id,
            version_id=job.version_id,
            status=JobStatus.SUCCEEDED,
            chunks_created_count=chunks,
            entities_created_count=entities,
        )

    return handler


def _fails(kind: str):
    async def handler(session, job: JobRef) -> JobOutcome:
        return JobOutcome(
            job_id=job.job_id,
            version_id=job.version_id,
            status=JobStatus.FAILED,
            error_details=f"{kind}: no",
            failure_kind=kind,
        )

    return handler


def _raises(exc: Exception):
    async def handler(session, job: JobRef) -> JobOutcome:
        raise exc

    return handler


# ---------------------------------------------------------------------------
# Claim and dispatch
# ---------------------------------------------------------------------------

class TestPollOnce:
    async def test_an_empty_queue_returns_none(self, session_factory):
        assert await poll_once(_deps(session_factory, _succeeds())) is None

    async def test_a_successful_job_is_recorded_with_its_counts(self, session_factory):
        job_id = await _add_job(session_factory)

        outcome = await poll_once(_deps(session_factory, _succeeds(chunks=7, entities=5)))

        row = await _row(session_factory, job_id)
        assert outcome.status is JobStatus.SUCCEEDED
        assert row.status == "SUCCEEDED"
        assert row.chunks_created_count == 7
        assert row.entities_created_count == 5
        assert row.completed_at is not None

    async def test_the_handler_is_chosen_by_job_type(self, session_factory):
        """A DELETE job must not run the ingestion pipeline. The dispatch is a
        table lookup precisely so this cannot drift into an if/elif."""
        await _add_job(session_factory, job_type="DELETE")
        seen: list[JobType] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            seen.append(job.job_type)
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await poll_once(_deps(session_factory, handler))

        assert seen == [JobType.DELETE]

    async def test_the_claimed_job_is_handed_to_the_handler(self, session_factory):
        job_id = await _add_job(session_factory, attempt_count=2)
        seen: list[JobRef] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            seen.append(job)
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await poll_once(_deps(session_factory, handler))

        assert seen[0].job_id == job_id
        # The retry decision needs this, and re-reading the row to get it
        # would race with another worker.
        assert seen[0].attempt_count == 2

    async def test_only_one_job_is_taken_per_tick(self, session_factory):
        await _add_job(session_factory)
        await _add_job(session_factory)

        await poll_once(_deps(session_factory, _succeeds()))

        async with session_factory() as session:
            from sqlalchemy import func, select
            still_queued = (
                await session.execute(
                    select(func.count()).select_from(KnowledgeInjectionJob).where(
                        KnowledgeInjectionJob.status == "QUEUED"
                    )
                )
            ).scalar_one()
        assert still_queued == 1


# ---------------------------------------------------------------------------
# A crash must not strand the job
# ---------------------------------------------------------------------------

class TestHandlerCrash:
    async def test_a_crashing_handler_does_not_leave_the_job_running(
        self, session_factory
    ):
        """The reason the last-resort catch exists. `claim_next_job` skips any
        source that already has a RUNNING job, so a job stranded in RUNNING
        blocks that source's ingestion permanently — until the reaper, half an
        hour later, notices."""
        job_id = await _add_job(session_factory)

        outcome = await poll_once(_deps(session_factory, _raises(RuntimeError("boom"))))

        row = await _row(session_factory, job_id)
        assert row.status != "RUNNING"
        assert row.status == "FAILED"
        assert outcome is not None

    async def test_a_crash_is_terminal_rather_than_retried(self, session_factory):
        """An unhandled exception is a defect. Retrying it three more times
        turns one stack trace into four and changes nothing."""
        job_id = await _add_job(session_factory)

        await poll_once(_deps(session_factory, _raises(RuntimeError("boom"))))

        row = await _row(session_factory, job_id)
        assert row.failure_kind == "unhandled_exception"
        assert row.next_attempt_at is None
        assert row.attempt_count == 1

    async def test_the_exception_text_reaches_the_row(self, session_factory):
        job_id = await _add_job(session_factory)

        await poll_once(_deps(session_factory, _raises(ValueError("tika exploded"))))

        assert "tika exploded" in (await _row(session_factory, job_id)).error_details

    async def test_the_loop_survives_a_crashing_job(self, session_factory):
        """One poisonous document must not stop the queue behind it.

        Keyed on the job id rather than call order: claims are ordered by
        `job_id` among rows with no `started_at`, so "the first one claimed"
        is whichever uuid sorts lower, not whichever was inserted first.
        """
        poison_id = await _add_job(session_factory)
        good_id = await _add_job(session_factory)

        async def handler(session, job: JobRef) -> JobOutcome:
            if job.job_id == poison_id:
                raise RuntimeError("boom")
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await run_worker(_deps(session_factory, handler), max_iterations=2)

        assert (await _row(session_factory, poison_id)).status == "FAILED"
        assert (await _row(session_factory, good_id)).status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# The status poll_once reports is the one that was written
# ---------------------------------------------------------------------------

class TestOutcomeReflectsThePolicy:
    async def test_a_transient_failure_is_reported_as_requeued(self, session_factory):
        """The handler said FAILED; the policy wrote QUEUED. Returning the
        handler's answer would tell the caller the document had stopped when
        it is going to be tried again."""
        job_id = await _add_job(session_factory)

        outcome = await poll_once(
            _deps(session_factory, _fails("eav_extraction_rate_limited"))
        )

        row = await _row(session_factory, job_id)
        assert outcome.status is JobStatus.QUEUED
        assert row.status == "QUEUED"
        assert row.next_attempt_at is not None

    async def test_a_terminal_failure_is_reported_as_failed(self, session_factory):
        job_id = await _add_job(session_factory)

        outcome = await poll_once(_deps(session_factory, _fails("checksum_mismatch")))

        assert outcome.status is JobStatus.FAILED
        assert (await _row(session_factory, job_id)).status == "FAILED"

    async def test_an_exhausted_job_is_reported_as_dead_lettered(self, session_factory):
        job_id = await _add_job(
            session_factory, attempt_count=retry.MAX_ATTEMPTS - 1
        )

        outcome = await poll_once(
            _deps(session_factory, _fails("eav_extraction_rate_limited"))
        )

        assert outcome.status is JobStatus.DEAD_LETTER
        assert (await _row(session_factory, job_id)).status == "DEAD_LETTER"

    async def test_a_requeued_job_is_not_claimed_again_immediately(
        self, session_factory
    ):
        """Otherwise "retry in ten minutes" means "retry on the next poll" —
        a busy-wait against whatever was throttling us."""
        await _add_job(session_factory)

        first = await poll_once(_deps(session_factory, _fails("tika_transient")))
        second = await poll_once(_deps(session_factory, _fails("tika_transient")))

        assert first is not None and first.status is JobStatus.QUEUED
        assert second is None


# ---------------------------------------------------------------------------
# Startup and the loop itself
# ---------------------------------------------------------------------------

class TestRunWorker:
    async def test_stale_jobs_are_reaped_before_any_work(self, session_factory):
        """A worker killed mid-job leaves a row RUNNING, and the guard then
        blocks that source forever. Reaping has to happen at startup, before
        the first claim, or the first tick claims nothing and sleeps."""
        source_id = uuid.uuid4()
        stale_id = await _add_job(
            session_factory,
            source_id=source_id,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )

        await run_worker(_deps(session_factory, _succeeds()), max_iterations=1)

        # Reaped, then immediately claimed and run by the same first tick.
        assert (await _row(session_factory, stale_id)).status == "SUCCEEDED"

    async def test_reaping_reports_how_many_it_requeued(self, session_factory):
        await _add_job(
            session_factory,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )

        assert await reap_stale_jobs(_deps(session_factory, _succeeds())) == 1

    async def test_a_healthy_workers_job_is_left_alone(self, session_factory):
        """`stale_job_seconds` must exceed the longest legitimate job, or this
        yanks a document out from under a worker still processing it."""
        job_id = await _add_job(
            session_factory,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        )

        await reap_stale_jobs(_deps(session_factory, _succeeds()))

        assert (await _row(session_factory, job_id)).status == "RUNNING"

    async def test_the_loop_is_bounded_by_max_iterations(self, session_factory):
        for _ in range(5):
            await _add_job(session_factory)

        await run_worker(_deps(session_factory, _succeeds()), max_iterations=2)

        async with session_factory() as session:
            from sqlalchemy import func, select
            done = (
                await session.execute(
                    select(func.count()).select_from(KnowledgeInjectionJob).where(
                        KnowledgeInjectionJob.status == "SUCCEEDED"
                    )
                )
            ).scalar_one()
        assert done == 2

    async def test_an_empty_queue_sleeps_rather_than_spinning(
        self, session_factory, monkeypatch
    ):
        """Without the sleep the poll is a hot loop against Postgres."""
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await run_worker(
            _deps(session_factory, _succeeds(), poll_interval=2.0), max_iterations=3
        )

        assert slept == [2.0, 2.0, 2.0]

    async def test_a_tick_that_found_work_does_not_sleep(
        self, session_factory, monkeypatch
    ):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await _add_job(session_factory)

        await run_worker(
            _deps(session_factory, _succeeds(), poll_interval=2.0), max_iterations=1
        )

        assert slept == []


# ---------------------------------------------------------------------------
# The one-job-per-source guard
# ---------------------------------------------------------------------------

class TestAbandonedJobsResumeImmediately:
    async def test_a_reaped_job_is_claimable_on_the_same_tick(self, session_factory):
        """Regression. The retry work gave every transient failure a ten-minute
        backoff, and `worker_abandoned` is transient — so an ordinary deploy
        silently cost every in-flight document ten idle minutes before it
        resumed. A dead worker is not a condition that needs time to clear:
        the process doing the reaping is its replacement."""
        job_id = await _add_job(
            session_factory,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )

        await reap_stale_jobs(_deps(session_factory, _succeeds()))

        row = await _row(session_factory, job_id)
        assert row.status == "QUEUED"
        assert row.next_attempt_at is None      # ready now, not in ten minutes
        assert row.attempt_count == 1           # but it still cost an attempt


class TestOneJobPerSource:
    async def test_a_source_with_a_running_job_is_skipped(self, session_factory):
        """Two workers ingesting the same document would race on the same
        version's chunks. The guard is what makes `FOR UPDATE SKIP LOCKED`
        safe to point at more than one worker."""
        source_id = uuid.uuid4()
        await _add_job(
            session_factory,
            source_id=source_id,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        await _add_job(session_factory, source_id=source_id, status="QUEUED")

        assert await poll_once(_deps(session_factory, _succeeds())) is None

    async def test_another_source_is_still_claimable(self, session_factory):
        """The guard is per source, not global — one busy document must not
        stop every other one."""
        await _add_job(
            session_factory,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        other_id = await _add_job(session_factory, status="QUEUED")

        outcome = await poll_once(_deps(session_factory, _succeeds()))

        assert outcome is not None and outcome.job_id == other_id


# ---------------------------------------------------------------------------
# Concurrent lanes (ingestion.md P2 item 12)
#
# Lanes inside one process, not several processes: a second process would load
# its own 400 MB copy of the embedding model, which is what made concurrency
# look unaffordable before the model was hoisted to process scope.
# ---------------------------------------------------------------------------

class TestConcurrentLanes:
    async def test_two_lanes_process_two_documents_at_once(self, session_factory):
        """The property that matters: the second document starts before the
        first has finished.

        A rendezvous rather than a sleep. Counting yields is a race the test
        can lose for reasons that say nothing about the code -- the claim
        itself awaits the database several times, so a handler that yields
        twice may well finish before the other lane is past its own claim.
        Here the first handler cannot proceed until the second arrives, so
        with one lane it deadlocks and the timeout fails the test loudly.
        """
        await _add_job(session_factory)
        await _add_job(session_factory)
        both_in_flight = asyncio.Event()
        arrived = {"n": 0}

        async def handler(session, job: JobRef) -> JobOutcome:
            arrived["n"] += 1
            if arrived["n"] == 2:
                both_in_flight.set()
            await asyncio.wait_for(both_in_flight.wait(), timeout=5)
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await run_worker(
            _deps(session_factory, handler, concurrency=2), max_iterations=1
        )

        assert arrived["n"] == 2
        assert both_in_flight.is_set()

    async def test_one_slow_document_does_not_hold_the_queue_head(
        self, session_factory
    ):
        """The real benefit. Extraction is network-bound on a provider with a
        daily ceiling, so lanes do not double throughput against it — what
        they do is stop a slow document blocking everything behind it."""
        slow_id = await _add_job(session_factory)
        fast_id = await _add_job(session_factory)
        fast_done = asyncio.Event()
        finished: list[uuid.UUID] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            if job.job_id == slow_id:
                # Held until the other document has been through end to end.
                # Serially this could never resolve.
                await asyncio.wait_for(fast_done.wait(), timeout=5)
            finished.append(job.job_id)
            if job.job_id == fast_id:
                fast_done.set()
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await run_worker(
            _deps(session_factory, handler, concurrency=2), max_iterations=1
        )

        assert set(finished) == {slow_id, fast_id}
        assert finished[0] == fast_id      # the fast one did not wait

    async def test_two_lanes_never_take_the_same_job(self, session_factory):
        """`FOR UPDATE SKIP LOCKED` hands each lane a different row. Running
        one document twice would race on the same version's chunks."""
        for _ in range(4):
            await _add_job(session_factory)
        seen: list[uuid.UUID] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            seen.append(job.job_id)
            await asyncio.sleep(0)
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await run_worker(
            _deps(session_factory, handler, concurrency=2), max_iterations=2
        )

        assert len(seen) == len(set(seen))

    async def test_two_lanes_never_take_two_jobs_for_one_source(
        self, session_factory
    ):
        """The one-job-per-source guard is what makes lanes safe. Without it
        two lanes could ingest two versions of the same document at once."""
        source_id = uuid.uuid4()
        for _ in range(2):
            await _add_job(session_factory, source_id=source_id)
        concurrent_for_source = {"peak": 0, "now": 0}

        async def handler(session, job: JobRef) -> JobOutcome:
            concurrent_for_source["now"] += 1
            concurrent_for_source["peak"] = max(
                concurrent_for_source["peak"], concurrent_for_source["now"]
            )
            await asyncio.sleep(0)
            concurrent_for_source["now"] -= 1
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await run_worker(
            _deps(session_factory, handler, concurrency=2), max_iterations=1
        )

        assert concurrent_for_source["peak"] == 1

    async def test_concurrency_of_one_is_exactly_the_old_behaviour(
        self, session_factory
    ):
        """The escape hatch: PGQUEUE_CONCURRENCY=1 restores strictly serial
        processing, and `max_iterations` then means what it always did."""
        for _ in range(3):
            await _add_job(session_factory)

        await run_worker(
            _deps(session_factory, _succeeds(), concurrency=1), max_iterations=2
        )

        async with session_factory() as session:
            from sqlalchemy import func, select
            done = (
                await session.execute(
                    select(func.count()).select_from(KnowledgeInjectionJob).where(
                        KnowledgeInjectionJob.status == "SUCCEEDED"
                    )
                )
            ).scalar_one()
        assert done == 2


# ---------------------------------------------------------------------------
# Trace ids (ingestion.md P3 item 14)
# ---------------------------------------------------------------------------

class TestTraceLabelling:
    async def test_every_line_of_a_run_carries_the_same_id(self, session_factory):
        """The point of the ContextVar: lines logged from modules that know
        nothing about jobs still carry the run's id."""
        from logging_config import current_trace_id

        await _add_job(session_factory)
        seen: list[str | None] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            seen.append(current_trace_id())
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await poll_once(_deps(session_factory, handler))

        assert seen[0] is not None

    async def test_the_id_names_the_run_not_the_job(self, session_factory):
        """A job can be retried four times. `job_id` alone would give all four
        runs the same label, which is exactly what makes a log unreadable."""
        from logging_config import current_trace_id

        job_id = await _add_job(session_factory, attempt_count=2)
        seen: list[str | None] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            seen.append(current_trace_id())
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await poll_once(_deps(session_factory, handler))

        # Third attempt of this job, and the job's own prefix is kept so one
        # grep still finds every attempt.
        assert seen[0] == f"{job_id.hex[:8]}.3"

    async def test_two_lanes_get_different_ids(self, session_factory):
        """Without this, interleaved lanes are an unreadable stream."""
        from logging_config import current_trace_id

        await _add_job(session_factory)
        await _add_job(session_factory)
        seen: list[str | None] = []

        async def handler(session, job: JobRef) -> JobOutcome:
            await asyncio.sleep(0)
            seen.append(current_trace_id())
            return JobOutcome(
                job_id=job.job_id, version_id=job.version_id,
                status=JobStatus.SUCCEEDED,
            )

        await run_worker(
            _deps(session_factory, handler, concurrency=2), max_iterations=1
        )

        assert len(seen) == 2
        assert seen[0] != seen[1]

    async def test_the_context_does_not_leak_past_the_run(self, session_factory):
        from logging_config import current_trace_id

        await _add_job(session_factory)
        await poll_once(_deps(session_factory, _succeeds()))

        assert current_trace_id() is None

    async def test_an_unlabelled_line_still_formats(self):
        """A format string naming `trace_id` raises inside logging on a record
        that lacks the attribute, so the filter has to cover every record —
        not only the ones emitted inside a run."""
        import logging

        from logging_config import NO_TRACE, TraceIdFilter

        record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
        assert TraceIdFilter().filter(record) is True
        assert record.trace_id == NO_TRACE

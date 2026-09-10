"""Retry policy and the dead-letter state (ingestion.md items 6 and 7).

Every ingestion failure used to be terminal: `complete_job` wrote FAILED and
the document stopped there, whatever the reason. That was worst for exactly
the most common failure -- provider throttling -- where waiting ten minutes is
the entire fix.

Two layers, tested separately because they fail differently. `retry.decide` is
a pure function and gets exhaustive cases; the repository half is tested
against real SQLite rows so the claim query's WHERE clause is actually
exercised rather than asserted about.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import KnowledgeInjectionJob
from ingestion.pipeline_types import JobStatus
from ingestion.queue import repository, retry


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

class TestDecide:
    def test_a_transient_failure_goes_back_on_the_queue(self):
        decision = retry.decide(
            failure_kind="eav_extraction_rate_limited", attempt_count=0
        )

        assert decision.status == "QUEUED"
        assert decision.will_retry is True
        assert decision.attempt_count == 1
        assert decision.next_attempt_at is not None

    def test_a_terminal_failure_is_not_retried(self):
        """Retrying a model that rejects the content produces the identical
        rejection and spends real tokens doing it."""
        decision = retry.decide(failure_kind="eav_extraction_failed", attempt_count=0)

        assert decision.status == "FAILED"
        assert decision.will_retry is False
        assert decision.next_attempt_at is None

    def test_a_checksum_mismatch_is_terminal(self):
        assert retry.decide(
            failure_kind="checksum_mismatch", attempt_count=0
        ).status == "FAILED"

    def test_an_unclassified_failure_is_terminal(self):
        """The safe direction. A failure kind nobody has classified yet stops
        after one attempt and stays visible, rather than quietly consuming a
        full retry budget on every job that hits it."""
        assert retry.decide(
            failure_kind="something_nobody_has_seen", attempt_count=0
        ).status == "FAILED"
        assert retry.decide(failure_kind=None, attempt_count=0).status == "FAILED"

    def test_the_last_attempt_dead_letters_rather_than_failing(self):
        """The distinction item 7 exists to draw: this kept failing for a
        reason that usually passes, which needs a different response from a
        human than "this cannot work as it stands"."""
        decision = retry.decide(
            failure_kind="eav_extraction_rate_limited",
            attempt_count=retry.MAX_ATTEMPTS - 1,
        )

        assert decision.status == "DEAD_LETTER"
        assert decision.will_retry is False
        assert decision.next_attempt_at is None

    def test_attempts_are_bounded(self):
        """Every retry re-runs the whole pipeline. Against a daily token quota
        a job that retries forever spends the budget the jobs that would
        succeed need."""
        statuses = [
            retry.decide(
                failure_kind="eav_extraction_rate_limited", attempt_count=n
            ).status
            for n in range(retry.MAX_ATTEMPTS + 3)
        ]
        assert statuses.count("QUEUED") == retry.MAX_ATTEMPTS - 1
        assert all(s == "DEAD_LETTER" for s in statuses[retry.MAX_ATTEMPTS - 1 :])

    def test_backoff_grows_and_is_capped(self):
        delays = [retry.backoff_seconds(n) for n in range(1, 8)]

        assert delays[0] == retry.BACKOFF_BASE_SECONDS
        assert delays == sorted(delays)                     # never shrinks
        assert max(delays) <= retry.BACKOFF_MAX_SECONDS

    def test_the_backoff_is_long_enough_to_outlast_a_rate_limit_window(self):
        """A per-minute token bucket refills every minute; retrying in five
        seconds just burns an attempt on the same throttle."""
        assert retry.backoff_seconds(1) >= 60

    def test_next_attempt_is_in_the_future(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        decision = retry.decide(
            failure_kind="tika_transient", attempt_count=0, now=now
        )
        assert decision.next_attempt_at > now


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

@pytest.fixture
async def session_factory():
    """Only `knowledge_injection_job`; its siblings carry JSONB and pgvector
    columns SQLite cannot render, and SQLite does not enforce the foreign
    keys."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(KnowledgeInjectionJob.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


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


async def _fail(factory, job_id, kind):
    async with factory() as session:
        return await repository.complete_job(
            session,
            job_id=job_id,
            status=JobStatus.FAILED,
            chunks_created_count=0,
            entities_created_count=0,
            error_details=f"{kind}: something went wrong",
            failure_kind=kind,
        )


class TestCompleteJobAppliesThePolicy:
    async def test_a_throttled_job_is_requeued_not_failed(self, session_factory):
        job_id = await _add_job(session_factory)

        written = await _fail(session_factory, job_id, "eav_extraction_rate_limited")

        row = await _row(session_factory, job_id)
        assert written is JobStatus.QUEUED
        assert row.status == "QUEUED"
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None

    async def test_a_requeued_job_is_not_marked_completed(self, session_factory):
        """A pending retry has not completed. A stale `completed_at` would
        sort it in among the finished work on the Jobs page."""
        job_id = await _add_job(session_factory)

        await _fail(session_factory, job_id, "eav_extraction_rate_limited")

        assert (await _row(session_factory, job_id)).completed_at is None

    async def test_a_terminal_failure_is_still_terminal(self, session_factory):
        job_id = await _add_job(session_factory)

        written = await _fail(session_factory, job_id, "eav_extraction_failed")

        row = await _row(session_factory, job_id)
        assert written is JobStatus.FAILED
        assert row.status == "FAILED"
        assert row.next_attempt_at is None
        assert row.completed_at is not None

    async def test_repeated_transient_failures_end_in_dead_letter(self, session_factory):
        job_id = await _add_job(session_factory)

        seen = []
        for _ in range(retry.MAX_ATTEMPTS):
            seen.append(await _fail(session_factory, job_id, "tika_transient"))

        row = await _row(session_factory, job_id)
        assert seen[-1] is JobStatus.DEAD_LETTER
        assert row.status == "DEAD_LETTER"
        assert row.attempt_count == retry.MAX_ATTEMPTS
        assert row.next_attempt_at is None
        assert row.completed_at is not None

    async def test_the_failure_kind_is_recorded(self, session_factory):
        """Item 13: grouping failures used to mean splitting `error_details`
        on its first colon in every caller."""
        job_id = await _add_job(session_factory)

        await _fail(session_factory, job_id, "eav_extraction_rate_limited")

        assert (
            await _row(session_factory, job_id)
        ).failure_kind == "eav_extraction_rate_limited"

    async def test_success_is_recorded_unchanged(self, session_factory):
        job_id = await _add_job(session_factory, attempt_count=2)

        async with session_factory() as session:
            written = await repository.complete_job(
                session,
                job_id=job_id,
                status=JobStatus.SUCCEEDED,
                chunks_created_count=5,
                entities_created_count=4,
                error_details=None,
            )

        row = await _row(session_factory, job_id)
        assert written is JobStatus.SUCCEEDED
        assert row.status == "SUCCEEDED"
        assert row.chunks_created_count == 5
        assert row.next_attempt_at is None


class TestBackoffIsHonoured:
    async def test_a_job_waiting_out_its_backoff_is_not_claimed(self, session_factory):
        """Without this the retry would be claimed on the very next poll --
        a busy-wait against whatever was throttling us."""
        await _add_job(
            session_factory,
            next_attempt_at=datetime.now(UTC).replace(tzinfo=None)
            + timedelta(minutes=10),
        )

        async with session_factory() as session:
            assert await repository.claim_next_job(session) is None

    async def test_a_job_whose_backoff_has_elapsed_is_claimed(self, session_factory):
        job_id = await _add_job(
            session_factory,
            next_attempt_at=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(minutes=1),
        )

        async with session_factory() as session:
            claimed = await repository.claim_next_job(session)

        assert claimed is not None
        assert claimed.job_id == job_id

    async def test_a_job_that_never_failed_is_claimed(self, session_factory):
        """NULL means ready now, which is why no backfill was needed for the
        rows already in the database."""
        job_id = await _add_job(session_factory, next_attempt_at=None)

        async with session_factory() as session:
            claimed = await repository.claim_next_job(session)

        assert claimed is not None and claimed.job_id == job_id

    async def test_claiming_clears_the_backoff(self, session_factory):
        job_id = await _add_job(
            session_factory,
            next_attempt_at=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(minutes=1),
        )

        async with session_factory() as session:
            await repository.claim_next_job(session)

        assert (await _row(session_factory, job_id)).next_attempt_at is None

    async def test_the_attempt_count_reaches_the_handler(self, session_factory):
        """The retry decision needs it, and reading the row a second time to
        get it would race with another worker."""
        job_id = await _add_job(session_factory, attempt_count=2)

        async with session_factory() as session:
            claimed = await repository.claim_next_job(session)

        assert claimed.attempt_count == 2


class TestAbandonedJobsAreCounted:
    async def test_a_worker_death_consumes_an_attempt(self, session_factory):
        """Otherwise a document that kills the worker every time is requeued
        forever, and the reaper hands it a fresh worker to kill each time."""
        job_id = await _add_job(
            session_factory,
            status="RUNNING",
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )

        async with session_factory() as session:
            assert await repository.reset_stale_running_jobs(
                session, older_than_seconds=1800
            ) == 1

        row = await _row(session_factory, job_id)
        assert row.status == "QUEUED"
        assert row.attempt_count == 1
        assert row.failure_kind == retry.WORKER_ABANDONED

    async def test_a_job_that_keeps_killing_the_worker_is_dead_lettered(
        self, session_factory
    ):
        job_id = await _add_job(
            session_factory,
            status="RUNNING",
            attempt_count=retry.MAX_ATTEMPTS - 1,
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )

        async with session_factory() as session:
            await repository.reset_stale_running_jobs(session, older_than_seconds=1800)

        row = await _row(session_factory, job_id)
        assert row.status == "DEAD_LETTER"
        assert row.completed_at is not None

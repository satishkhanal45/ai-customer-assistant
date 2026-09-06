"""Tests for the stale-RUNNING-job reaper (P1-5).

A worker killed mid-job leaves its row RUNNING forever. `claim_next_job`
skips any source that already has a RUNNING job, so a single abandoned row
silently blocks that source's ingestion permanently. `reset_stale_running_jobs`
is what unblocks it, and it runs once at worker startup.

Backed by real SQLite rows rather than mocks, so the query's WHERE clause is
actually exercised.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import KnowledgeInjectionJob
from ingestion.queue import repository


@pytest.fixture
async def session_factory():
    """Only `knowledge_injection_job` is created.

    The reaper reads and writes that one table, and the sibling tables carry
    types SQLite cannot render (`knowledge_source_version.metadata` is JSONB,
    `embedding_chunk.embedding` is a pgvector column). SQLite does not enforce
    foreign keys by default, so job rows insert fine without their parents.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(KnowledgeInjectionJob.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _add_job(factory, *, status: str, started_minutes_ago: float | None) -> uuid.UUID:
    job_id = uuid.uuid4()
    started = (
        None
        if started_minutes_ago is None
        else datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=started_minutes_ago)
    )
    async with factory() as session:
        session.add(
            KnowledgeInjectionJob(
                job_id=job_id, source_id=uuid.uuid4(), version_id=uuid.uuid4(),
                job_type="INITIAL_INGEST", status=status, started_at=started,
                triggered_by="test",
            )
        )
        await session.commit()
    return job_id


async def _status_of(factory, job_id: uuid.UUID) -> str:
    async with factory() as session:
        return (await session.get(KnowledgeInjectionJob, job_id)).status


class TestResetStaleRunningJobs:
    async def test_requeues_a_job_abandoned_long_ago(self, session_factory):
        job_id = await _add_job(session_factory, status="RUNNING", started_minutes_ago=60)

        async with session_factory() as session:
            requeued = await repository.reset_stale_running_jobs(
                session, older_than_seconds=1800
            )

        assert requeued == 1
        assert await _status_of(session_factory, job_id) == "QUEUED"

    async def test_leaves_a_recently_started_job_alone(self, session_factory):
        """A healthy worker is still processing this one — yanking it away
        would run the same document twice."""
        job_id = await _add_job(session_factory, status="RUNNING", started_minutes_ago=2)

        async with session_factory() as session:
            requeued = await repository.reset_stale_running_jobs(
                session, older_than_seconds=1800
            )

        assert requeued == 0
        assert await _status_of(session_factory, job_id) == "RUNNING"

    @pytest.mark.parametrize("status", ["QUEUED", "SUCCEEDED", "FAILED"])
    async def test_ignores_jobs_that_are_not_running(self, session_factory, status):
        job_id = await _add_job(session_factory, status=status, started_minutes_ago=999)

        async with session_factory() as session:
            requeued = await repository.reset_stale_running_jobs(
                session, older_than_seconds=1800
            )

        assert requeued == 0
        assert await _status_of(session_factory, job_id) == status

    async def test_clears_started_at_so_the_row_is_claimable_again(self, session_factory):
        job_id = await _add_job(session_factory, status="RUNNING", started_minutes_ago=60)

        async with session_factory() as session:
            await repository.reset_stale_running_jobs(session, older_than_seconds=1800)

        async with session_factory() as session:
            row = await session.get(KnowledgeInjectionJob, job_id)
        assert row.started_at is None
        assert "requeued" in row.error_details

    async def test_empty_queue_is_a_no_op(self, session_factory):
        async with session_factory() as session:
            assert await repository.reset_stale_running_jobs(
                session, older_than_seconds=1800
            ) == 0

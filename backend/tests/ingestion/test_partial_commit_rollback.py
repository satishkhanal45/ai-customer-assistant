"""A failed ingestion must not commit the work it staged (P2-3).

`_stage_persist` writes chunks and EAV rows with `session.add` / `flush` and
no commit of its own. When a later stage failed, the `Err` branch called
`mark_version_status(..., FAILED)` — which *does* commit — and so carried the
partial writes into the database alongside the failure status.

The damage was quiet rather than dramatic: the retrieval join contract filters
on `current_version_id` + `INDEXED`, so orphan chunks never surfaced in
search. They just accumulated, inflated `chunks_created_count`, and made the
tables misleading to anyone reading them directly.

These tests drive `run_ingestion` with a session that records the order of
`flush` / `rollback` / `commit`, because the ordering *is* the fix: a rollback
that happens after the status commit would be useless.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

# `ingestion.queue.__init__` imports the worker, which imports `pipeline`, so
# importing `pipeline` first hits a partially-initialised module. Importing the
# queue package first breaks the cycle; the sibling ingestion tests do the same.
import ingestion.queue  # noqa: F401
from ingestion import pipeline
from ingestion.pipeline_types import JobRef, JobStatus, JobType, VersionStatus


class _RecordingSession:
    """Records the transaction verbs in the order they are called."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def flush(self) -> None:
        self.calls.append("flush")


@dataclass
class _FakeSource:
    source_id: uuid.UUID
    current_version_id: uuid.UUID | None = None
    source_name: str = "doc"


@dataclass
class _FakeVersion:
    version_id: uuid.UUID


@pytest.fixture
def job() -> JobRef:
    return JobRef(
        job_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        job_type=JobType.INITIAL_INGEST,
        status=JobStatus.RUNNING,
        triggered_by="test",
    )


@pytest.fixture
def repo(monkeypatch, job):
    """Stub the repository so the test exercises `run_ingestion`'s own
    transaction handling rather than a live database."""
    recorded: dict = {"statuses": []}

    async def _load_source(session, source_id):
        return _FakeSource(source_id=source_id)

    async def _load_version(session, version_id):
        return _FakeVersion(version_id=version_id)

    async def _mark_version_status(session, version_id, status):
        recorded["statuses"].append(status)
        await session.commit()

    async def _cutover(session, **kwargs):
        await session.commit()

    monkeypatch.setattr(pipeline.job_repo, "load_source", _load_source)
    monkeypatch.setattr(pipeline.job_repo, "load_version", _load_version)
    monkeypatch.setattr(pipeline.job_repo, "mark_version_status", _mark_version_status)
    monkeypatch.setattr(pipeline.job_repo, "cutover", _cutover)
    return recorded


class TestFailedIngestionRollsBack:
    async def test_rollback_happens_before_the_failure_status_is_committed(
        self, monkeypatch, job, repo
    ):
        from ingestion.result import Err, Ok

        session = _RecordingSession()

        async def staging(ctx):
            await session.flush()  # stands in for persist_chunks / persist_extraction
            return Ok(ctx)

        async def failing(ctx):
            return Err("eav_extraction_failed", "model returned nothing usable")

        monkeypatch.setattr(
            pipeline, "_STAGE_BUILDERS", (lambda deps: staging, lambda deps: failing)
        )

        outcome = await pipeline.run_ingestion(session, job, deps=object())

        assert outcome.status is JobStatus.FAILED
        assert repo["statuses"] == [VersionStatus.FAILED]
        # The staged write is discarded, and it is discarded *first*. A
        # rollback after the commit would be a no-op on already-durable rows.
        assert session.calls == ["flush", "rollback", "commit"]

    async def test_a_successful_run_does_not_roll_anything_back(
        self, monkeypatch, job, repo
    ):
        from ingestion.result import Ok

        session = _RecordingSession()

        async def staging(ctx):
            await session.flush()
            return Ok(ctx)

        monkeypatch.setattr(pipeline, "_STAGE_BUILDERS", (lambda deps: staging,))

        outcome = await pipeline.run_ingestion(session, job, deps=object())

        assert outcome.status is JobStatus.SUCCEEDED
        assert "rollback" not in session.calls

    async def test_a_failure_before_persist_is_also_safe(self, monkeypatch, job, repo):
        """Rolling back a transaction that staged nothing must not raise —
        most ingestion failures happen in fetch or extraction, before any
        write, and they take the same branch."""
        from ingestion.result import Err

        session = _RecordingSession()

        async def failing(ctx):
            return Err("fetch_failed", "404")

        monkeypatch.setattr(pipeline, "_STAGE_BUILDERS", (lambda deps: failing,))

        outcome = await pipeline.run_ingestion(session, job, deps=object())

        assert outcome.status is JobStatus.FAILED
        assert outcome.error_details == "fetch_failed: 404"
        assert session.calls == ["rollback", "commit"]

"""The extraction stage is bounded.

Found live on 2026-09-09: one document sat in RUNNING for 28 minutes with no
log output and no progress, and because the worker finishes one job before
claiming another, the entire queue stopped behind it. The provider client
had no timeout and the stage had no budget, so a single hung HTTP call
became an indefinitely stalled pipeline. The stale-job reaper could not
help either -- it runs once at worker startup, so a worker that is alive and
stuck blocks forever.

What matters is not the number but the property: a stage that does not
return must not be able to hold the queue open, and the job it belongs to
must fail in a way that is distinguishable from the model refusing the work.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ingestion.pipeline import _stage_eav_extraction
from ingestion.result import Err, Ok


class _Deps:
    """Only the two fields this stage touches."""

    def __init__(self, agent):
        self.extraction_agent = agent


@dataclass
class _Source:
    source_name: str = "doc.pdf"


@dataclass
class _Ctx:
    """A dataclass, because the happy path calls `dataclasses.replace` on it."""

    chunks: tuple = ()
    source: _Source = field(default_factory=_Source)
    chunk_extractions: tuple = ()


def _ctx_with(n_chunks: int = 3) -> _Ctx:
    return _Ctx(chunks=tuple(object() for _ in range(n_chunks)))


class TestStageBudget:
    async def test_a_stalled_extraction_fails_instead_of_hanging(self, monkeypatch):
        """The regression. A call that never returns must not hold the stage."""
        monkeypatch.setattr(
            "ingestion.pipeline.INGEST_EXTRACTION_STAGE_BUDGET_S", 0.2
        )

        def never_returns(*args, **kwargs):
            # Long relative to the 0.2s budget, short enough that the
            # orphaned thread does not hold up the suite -- `wait_for`
            # abandons the await but cannot kill the thread.
            time.sleep(3)

        monkeypatch.setattr("ingestion.pipeline.extract_document", never_returns)

        started = time.monotonic()
        result = await _stage_eav_extraction(_Deps(object()))(_ctx_with())
        elapsed = time.monotonic() - started

        assert isinstance(result, Err)
        assert elapsed < 5, f"stage took {elapsed:.1f}s; the budget did not apply"

    async def test_a_timeout_is_reported_as_its_own_kind(self, monkeypatch):
        """"This never came back" and "the model refused this" are different
        problems and should not share a failure string -- the Admin > Jobs
        page groups on it."""
        monkeypatch.setattr(
            "ingestion.pipeline.INGEST_EXTRACTION_STAGE_BUDGET_S", 0.1
        )
        monkeypatch.setattr(
            "ingestion.pipeline.extract_document", lambda *a, **k: time.sleep(3)
        )

        result = await _stage_eav_extraction(_Deps(object()))(_ctx_with(n_chunks=7))

        assert result.reason == "eav_extraction_timeout"
        assert "7 chunk" in result.detail

    async def test_a_provider_error_is_still_its_own_kind(self, monkeypatch):
        """The timeout branch must not swallow ordinary failures -- the 400
        JSON-validation errors seen in production are not timeouts and must
        stay distinguishable from them."""
        def explodes(*args, **kwargs):
            raise RuntimeError("Error code: 400 - Failed to validate JSON")

        monkeypatch.setattr("ingestion.pipeline.extract_document", explodes)

        result = await _stage_eav_extraction(_Deps(object()))(_ctx_with())

        assert result.reason == "eav_extraction_failed"
        assert "400" in result.detail

    async def test_a_normal_extraction_still_passes_through(self, monkeypatch):
        """The bound must not change the happy path."""
        monkeypatch.setattr(
            "ingestion.pipeline.extract_document", lambda *a, **k: ("extraction",)
        )

        result = await _stage_eav_extraction(_Deps(object()))(_ctx_with())

        assert isinstance(result, Ok)
        assert result.value.chunk_extractions == ("extraction",)


class TestClientBound:
    def test_the_provider_client_carries_a_timeout(self, monkeypatch):
        """The stage budget releases the *job*; only the client's own timeout
        ends the orphaned HTTP call, because Python cannot kill the thread
        `to_thread` is running. Both are needed."""
        import ingestion.pipeline as pipeline
        from timeouts import (
            INGEST_EXTRACTION_CALL_TIMEOUT_S,
            INGEST_EXTRACTION_MAX_RETRIES,
        )

        captured = {}

        class _FakeGroq:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        monkeypatch.setattr("groq.Groq", _FakeGroq)
        monkeypatch.setattr(
            "ingestion.extraction.agent.build_extraction_agent",
            lambda client, model: (client, model),
        )

        pipeline._default_extraction_agent()

        assert captured["timeout"] == INGEST_EXTRACTION_CALL_TIMEOUT_S
        assert captured["max_retries"] == INGEST_EXTRACTION_MAX_RETRIES


class TestBudgetsAreSane:
    def test_ingestion_is_allowed_longer_than_a_chat_turn(self):
        """Ingestion is background work with nobody watching, so it may be
        slower than the interactive ladder -- but bounded, which is the whole
        point. If these ever invert, someone has confused the two."""
        from timeouts import (
            INGEST_EXTRACTION_CALL_TIMEOUT_S,
            INGEST_EXTRACTION_STAGE_BUDGET_S,
            LLM_SHORT_TIMEOUT_S,
            TURN_BUDGET_S,
        )

        assert INGEST_EXTRACTION_CALL_TIMEOUT_S > LLM_SHORT_TIMEOUT_S
        assert INGEST_EXTRACTION_STAGE_BUDGET_S > TURN_BUDGET_S
        assert INGEST_EXTRACTION_STAGE_BUDGET_S > INGEST_EXTRACTION_CALL_TIMEOUT_S

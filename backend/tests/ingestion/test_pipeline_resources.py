"""Expensive pipeline resources are built once per process, not per job.

`_resolve_deps` used to construct everything per call, and the worker calls
it per job -- so the 400 MB BGE model and its tokenizer were loaded from disk
once per document. Both loaders document that they are deliberately not
memoized, leaving the caller to load once and inject; the caller did not. The
worker log showed `Loading weights: 199/199` on every single job, and
`pipeline.py`'s own docstring claimed the opposite was happening.

The `httpx.Client` was the same shape of bug with a different symptom: one
per job, never closed, a file descriptor leaked per document.
"""
from __future__ import annotations

import pytest

from ingestion import pipeline


@pytest.fixture(autouse=True)
def clean_resources():
    """The cache is process-global; a test that builds it must not leak into
    the next one."""
    pipeline.reset_pipeline_resources()
    yield
    pipeline.reset_pipeline_resources()


@pytest.fixture
def counting_build(monkeypatch):
    """Replace the real construction with a counter -- loading a real
    SentenceTransformer in a unit test would be the very cost under test."""
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def _build():
        calls["n"] += 1
        return pipeline.PipelineResources(
            http_client=_FakeClient(),
            tika_settings=object(),
            extraction_agent=object(),
            fetch_raw_bytes=lambda uri: None,
            chunk_and_embed=lambda doc: None,
        )

    monkeypatch.setattr(pipeline, "build_pipeline_resources", _build)
    return calls


class TestBuiltOnce:
    def test_the_first_call_builds(self, counting_build):
        pipeline.get_pipeline_resources()
        assert counting_build["n"] == 1

    def test_later_calls_reuse(self, counting_build):
        """The regression: this is once per job in production."""
        for _ in range(5):
            pipeline.get_pipeline_resources()
        assert counting_build["n"] == 1

    def test_the_same_object_comes_back(self, counting_build):
        assert pipeline.get_pipeline_resources() is pipeline.get_pipeline_resources()

    async def test_resolve_deps_reuses_them_across_jobs(self, counting_build):
        """`_resolve_deps` is what the worker calls per job. It may bind a
        new session each time; it must not rebuild the model."""
        first = await pipeline._resolve_deps(session="session-1")
        second = await pipeline._resolve_deps(session="session-2")

        assert counting_build["n"] == 1
        assert first.session == "session-1"
        assert second.session == "session-2"
        assert first.chunk_and_embed is second.chunk_and_embed
        assert first.http_client is second.http_client


class TestLifecycle:
    def test_reset_closes_the_http_client(self, counting_build):
        """One client per job, never closed, leaked a descriptor per
        document. Owning it means closing it."""
        client = pipeline.get_pipeline_resources().http_client
        pipeline.reset_pipeline_resources()
        assert client.closed is True

    def test_reset_forces_a_rebuild(self, counting_build):
        pipeline.get_pipeline_resources()
        pipeline.reset_pipeline_resources()
        pipeline.get_pipeline_resources()
        assert counting_build["n"] == 2

    def test_a_failing_close_does_not_propagate(self, monkeypatch):
        """Shutdown must not raise because a socket was already gone."""
        class _Angry:
            def close(self):
                raise OSError("already closed")

        monkeypatch.setattr(
            pipeline,
            "build_pipeline_resources",
            lambda: pipeline.PipelineResources(
                http_client=_Angry(),
                tika_settings=object(),
                extraction_agent=object(),
                fetch_raw_bytes=lambda uri: None,
                chunk_and_embed=lambda doc: None,
            ),
        )
        pipeline.get_pipeline_resources()
        pipeline.reset_pipeline_resources()          # must not raise


class TestCredentialWiring:
    def test_the_extraction_agent_uses_the_credential_store(self, monkeypatch):
        """A key saved on the Admin > API Keys page must reach ingestion.
        Reading os.environ directly here meant chat switched keys and the
        worker did not, with nothing saying so."""
        import llm_credentials

        captured = {}

        class _FakeGroq:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr("groq.Groq", _FakeGroq)
        monkeypatch.setattr(
            "ingestion.extraction.agent.build_extraction_agent",
            lambda client, model: (client, model),
        )
        monkeypatch.setattr(llm_credentials, "api_key_for", lambda provider: f"saved-{provider}")

        pipeline._default_extraction_agent()

        assert captured["api_key"] == "saved-groq"

    def test_it_still_falls_back_to_the_environment(self, monkeypatch):
        """A deployment that never opens that page behaves as before."""
        import llm_credentials

        captured = {}

        class _FakeGroq:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        llm_credentials._reset_for_tests()
        monkeypatch.setenv("GROQ_API_KEY", "from-the-environment")
        monkeypatch.setattr("groq.Groq", _FakeGroq)
        monkeypatch.setattr(
            "ingestion.extraction.agent.build_extraction_agent",
            lambda client, model: (client, model),
        )

        pipeline._default_extraction_agent()

        assert captured["api_key"] == "from-the-environment"

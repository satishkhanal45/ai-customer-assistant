"""Tests for Knowledge LLM providers (Phase 6, §4.6).

Covers the provider pattern copied from the Supervisor's ``llm_client.py``:

  - the deterministic stub whose output is directly parseable by the three
    Knowledge stage parsers (rewrite / extraction / answer);
  - provider resolution: explicit > config > stub, credential fallback, and
    unknown-provider errors;
  - ``llm_completions`` returning the exact three callables the Knowledge
    graph injects.
"""
from __future__ import annotations

import groq
import pytest

from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.extraction import _parse_response as parse_extraction
from agents.knowledge.llm import _parse_response as parse_answer
from agents.knowledge.providers import (
    StubKnowledgeProvider,
    _cooldown_seconds,
    build_knowledge_provider,
    llm_completions,
)
from agents.knowledge.rewriting import _parse_response as parse_rewrite


class TestCooldownParsing:
    def test_parses_minutes_and_seconds_hint(self) -> None:
        assert _cooldown_seconds("Please try again in 6m33.552s.") == pytest.approx(393.552, rel=1e-3)

    def test_parses_seconds_only(self) -> None:
        assert _cooldown_seconds("try again in 2s") == 2.0

    def test_returns_none_when_no_hint(self) -> None:
        assert _cooldown_seconds("Connection error.") is None


class TestStubProviderParseable:
    def test_rewrite_output_parses(self) -> None:
        parsed = parse_rewrite(
            StubKnowledgeProvider().rewrite_complete("What is the refund policy?"),
            original_text="What is the refund policy?",
        )
        assert parsed.rewritten_text == "customer's support question"
        assert parsed.resolved_references == ()

    def test_extraction_output_parses(self) -> None:
        parsed = parse_extraction(StubKnowledgeProvider().extraction_complete("any"))
        # All-None output is the expected "explain the RAG pipeline" signal:
        # vector-only retrieval, never an error.
        assert parsed.entity_type is None
        assert parsed.entity_label is None
        assert parsed.confidence == 0.0

    def test_answer_output_parses(self) -> None:
        parsed = parse_answer(StubKnowledgeProvider().answer_complete("s", "u"))
        assert parsed.answer
        assert parsed.is_grounded is False
        assert parsed.citation_indices == ()


class TestGroqProviderRetries:
    def test_retries_transient_errors_then_succeeds(self) -> None:
        from agents.knowledge.providers import GroqKnowledgeProvider

        class _FakeCompletions:
            def __init__(self) -> None:
                self.attempts = 0

            def create(self, **kwargs) -> object:
                self.attempts += 1
                if self.attempts < 3:
                    raise groq.APIConnectionError(request=None, message="Connection error.")
                return _FakeResponse("""{"ok": true}""")

        provider = GroqKnowledgeProvider.__new__(GroqKnowledgeProvider)
        provider.client = _FakeClient(_FakeCompletions())
        provider.model = provider.rewrite_model = "fake"
        provider.timeout = 0.1
        assert provider.rewrite_complete("prompt") == """{"ok": true}"""

    def test_raises_after_retries_exhausted(self) -> None:
        from agents.knowledge.providers import GroqKnowledgeProvider

        class _FakeCompletions:
            def create(self, **kwargs) -> object:
                raise groq.APIConnectionError(request=None, message="Connection error.")

        provider = GroqKnowledgeProvider.__new__(GroqKnowledgeProvider)
        provider.client = _FakeClient(_FakeCompletions())
        provider.model = provider.rewrite_model = "fake"
        provider.timeout = 0.1
        with pytest.raises(groq.APIConnectionError):
            provider.rewrite_complete("prompt")


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class _FakeClient:
    def __init__(self, completions) -> None:
        self.chat = type("C", (), {"completions": completions})()


class TestProviderResolution:
    def test_no_credentials_resolves_to_stub(self) -> None:
        assert isinstance(build_knowledge_provider(), StubKnowledgeProvider)

    def test_explicit_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError):
            build_knowledge_provider(provider="nonexistent")

    def test_config_default_anthropic_without_key_falls_back_to_stub(self) -> None:
        config = KnowledgeAgentConfig(llm_provider="anthropic")
        assert isinstance(build_knowledge_provider(config=config), StubKnowledgeProvider)

    def test_explicit_stub_with_config_is_stub(self) -> None:
        assert isinstance(
            build_knowledge_provider(provider="stub"),
            StubKnowledgeProvider,
        )


class TestLlmCompletionsShape:
    def test_exposes_exactly_the_three_graph_callables(self) -> None:
        completions = llm_completions(StubKnowledgeProvider())
        assert set(completions) == {
            "rewrite_llm_complete",
            "extraction_llm_complete",
            "answer_llm_complete",
        }
        for value in completions.values():
            assert callable(value)
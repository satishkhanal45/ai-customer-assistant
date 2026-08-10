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

import pytest

from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.extraction import _parse_response as parse_extraction
from agents.knowledge.llm import _parse_response as parse_answer
from agents.knowledge.providers import (
    StubKnowledgeProvider,
    build_knowledge_provider,
    llm_completions,
)
from agents.knowledge.rewriting import _parse_response as parse_rewrite


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
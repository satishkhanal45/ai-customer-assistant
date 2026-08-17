"""Tests for the Knowledge Agent's query-rewriting stage.

``rewrite_query`` is pure given an injected LLM callable, so these tests
use a fake completion — no network, matching the rest of the package.
The acronym-preservation rule is asserted against the real template file,
since a regression there silently breaks retrieval for short/abbreviated
queries (MVP, CRM, ...).
"""
from __future__ import annotations

import json

from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.constants import PROMPT_TEMPLATE_REWRITE
from agents.knowledge.rewriting import rewrite_query


def _load_rewrite_template(config: KnowledgeAgentConfig) -> str:
    template_path = config.prompts_dir / PROMPT_TEMPLATE_REWRITE
    assert template_path.exists(), f"missing {template_path}"
    return template_path.read_text(encoding="utf-8")


def test_rewrite_prompt_keeps_acronyms_verbatim():
    """Rule 6: acronyms like MVP/CRM must survive the rewrite unchanged —
    expanding them (e.g. to "minimum viable product") drops the rewritten
    query below the vector similarity threshold against source chunks that
    use the shorthand."""
    template = _load_rewrite_template(KnowledgeAgentConfig())
    assert "Keep acronyms and domain terms the customer used verbatim" in template
    assert '"MVP" stays "MVP"' in template


def test_rewrite_query_parses_fake_completion():
    config = KnowledgeAgentConfig()

    def fake_complete(prompt: str) -> str:
        assert "mvp development" in prompt
        return json.dumps(
            {
                "rewritten_text": "mvp development",
                "resolved_references": [],
            }
        )

    result = rewrite_query(
        "mvp development",
        (),
        config=config,
        llm_complete=fake_complete,
    )
    assert result.rewritten_text == "mvp development"
    assert result.resolved_references == ()
    assert result.original_text == "mvp development"


def test_rewrite_query_resolves_references():
    config = KnowledgeAgentConfig()

    def fake_complete(prompt: str) -> str:
        return json.dumps(
            {
                "rewritten_text": "what is Alpinist Studios's advisory board?",
                "resolved_references": ["it -> Alpinist Studios"],
            }
        )

    result = rewrite_query(
        "what is it?",
        ("Assistant: Alpinist Studios has an advisory board.",),
        config=config,
        llm_complete=fake_complete,
    )
    assert "Alpinist Studios" in result.rewritten_text
    assert result.resolved_references == ("it -> Alpinist Studios",)

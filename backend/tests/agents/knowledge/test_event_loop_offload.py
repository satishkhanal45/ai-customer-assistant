"""The Knowledge nodes must not block the event loop (P1-2).

The three LLM nodes are `async def`, so LangGraph runs them directly on the
event loop — unlike a sync node, which LangGraph offloads to a thread
executor. A blocking provider call inside one therefore freezes the entire
process: every other user's request, the health check, and any concurrently
running ingestion job, for the full duration of the call.

These tests assert the property rather than the implementation: the injected
completion runs on a *different* thread than the loop, and a slow completion
does not stop other coroutines from making progress.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.nodes import make_extract_node, make_llm_node, make_rewrite_node
from agents.knowledge.state import KnowledgeAgentState
from agents.knowledge.types import BuiltContext, BuiltPrompt, RewrittenQuery, StructuredQuery


def _config() -> KnowledgeAgentConfig:
    return KnowledgeAgentConfig()


_REWRITE_JSON = json.dumps({"rewritten_text": "restated", "resolved_references": []})
_EXTRACT_JSON = json.dumps(
    {
        "entity_type": None, "entity_label": None, "attribute": None,
        "relation_type": None, "filters": [], "confidence": 0.0,
    }
)
_ANSWER_JSON = json.dumps({"answer": "an answer", "is_grounded": True, "citation_indices": []})


def _rewrite_case():
    node = lambda completion: make_rewrite_node(config=_config(), llm_complete=completion)
    state = KnowledgeAgentState(raw_query="why", conversation_history=())
    return node, state, _REWRITE_JSON


def _extract_case():
    node = lambda completion: make_extract_node(config=_config(), llm_complete=completion)
    state = KnowledgeAgentState(
        raw_query="why",
        rewritten_query=RewrittenQuery(original_text="why", rewritten_text="why"),
    )
    return node, state, _EXTRACT_JSON


def _llm_case():
    node = lambda completion: make_llm_node(llm_complete=lambda system, user: completion(user))
    state = KnowledgeAgentState(
        raw_query="why",
        context=BuiltContext(structured_section="", documentation_section="", cited_provenance=()),
        prompt=BuiltPrompt(system_instructions="sys", rendered_prompt="usr"),
    )
    return node, state, _ANSWER_JSON


CASES = {"rewrite": _rewrite_case, "extract": _extract_case, "llm": _llm_case}


@pytest.mark.parametrize("case_name", list(CASES))
async def test_llm_call_runs_off_the_event_loop_thread(case_name):
    """The blocking provider call must happen on a worker thread."""
    make_node, state, payload = CASES[case_name]()
    loop_thread = threading.get_ident()
    ran_on: dict[str, int] = {}

    def completion(*_args, **_kwargs) -> str:
        ran_on["thread"] = threading.get_ident()
        return payload

    await make_node(completion)(state)

    assert ran_on["thread"] != loop_thread, (
        f"{case_name} node ran its blocking LLM call on the event loop thread"
    )


@pytest.mark.parametrize("case_name", list(CASES))
async def test_a_slow_llm_call_does_not_starve_other_coroutines(case_name):
    """The whole point: while one chat turn waits on the LLM, everything else
    in the process must keep running."""
    make_node, state, payload = CASES[case_name]()
    BLOCK_SECONDS = 0.4

    def slow_completion(*_args, **_kwargs) -> str:
        time.sleep(BLOCK_SECONDS)  # blocking, exactly like the Groq SDK
        return payload

    ticks = 0

    async def other_work() -> None:
        nonlocal ticks
        deadline = time.perf_counter() + BLOCK_SECONDS
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.02)
            ticks += 1

    await asyncio.gather(make_node(slow_completion)(state), other_work())

    # If the node blocked the loop, other_work() could not have run at all.
    assert ticks > 5, (
        f"event loop was starved during the {case_name} node's LLM call "
        f"(only {ticks} ticks in {BLOCK_SECONDS}s)"
    )

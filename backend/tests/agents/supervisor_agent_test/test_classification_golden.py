"""A golden set for classification, and the prompt's structural contract.

The Supervisor's system prompt is the single largest line item in a turn's
token cost, so it gets edited for size — and it is the one prompt whose
output nothing downstream can sanity-check, because `parse_llm_response`
degrades anything malformed to OUT_OF_SCOPE/UNKNOWN *silently*. A prompt
edit that made the model worse would therefore show up as "the assistant
started refusing things", with nothing in any log.

Two layers, mirroring how retrieval is guarded:

* **Structural tests** (always run, no network) assert that every rule the
  routing code depends on is still literally present in the prompt. These
  are what make a size-motivated edit safe to make.
* **A live golden set** (opt-in, skipped without GROQ_API_KEY) runs real
  classifications end to end, the same shape as the retrieval golden set
  in ``scripts/calibrate_retrieval.py``.

Run the live half with:

    GROQ_API_KEY=... pytest tests/agents/supervisor_agent_test/\
test_classification_golden.py -k live
"""

from __future__ import annotations

import os

import pytest

from agents.supervisor.classification import parse_llm_response
from agents.supervisor.prompt import (
    DOMAIN_DEFINITION,
    SUPERVISOR_SYSTEM_PROMPT,
    build_supervisor_system_prompt,
)
from agents.supervisor.schema import Intent, RequestCategory


# ---------------------------------------------------------------------------
# Structural contract — the rules the routing code cannot work without
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        # Every literal `parse_llm_response` maps. A prompt that stops
        # naming one of these degrades to OUT_OF_SCOPE/UNKNOWN in silence.
        "GREETING",
        "DOMAIN_REQUEST",
        "OUT_OF_SCOPE",
        "KNOWLEDGE_QUERY",
        "CREATE_TICKET",
        "CHECK_TICKET_STATUS",
        "UNKNOWN",
        # Every field name the parser reads.
        "request_category",
        "domain_confidence",
        "intent",
        "intent_confidence",
        "clarification_question",
    ],
)
def test_every_value_the_parser_understands_is_named(token):
    assert token in SUPERVISOR_SYSTEM_PROMPT


def test_the_domain_definition_placeholder_survives():
    """`build_supervisor_system_prompt` substitutes this by literal
    replacement; losing it would send the corpus-derived domain nowhere
    and silently make every question out of scope."""
    assert "{DOMAIN_DEFINITION}" in SUPERVISOR_SYSTEM_PROMPT
    rendered = build_supervisor_system_prompt("SOME DOMAIN")
    assert "SOME DOMAIN" in rendered
    assert "{DOMAIN_DEFINITION}" not in rendered


def test_the_clarification_threshold_is_stated():
    """`routing.decide_route` asks the clarifying question below 0.8. The
    prompt has to name the same number or the two disagree."""
    assert "0.8" in SUPERVISOR_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "rule",
    [
        # The discriminations that are genuinely hard and that worked
        # examples used to carry. Each must survive as a stated rule.
        "greeting",       # greeting + real question => DOMAIN_REQUEST
        "existing",       # existing ticket => CHECK_TICKET_STATUS
        "explicit",       # CREATE_TICKET only when explicit
        "latest",         # latest message wins over history
        "language",       # classify by meaning, not language
    ],
)
def test_the_hard_discriminations_are_still_described(rule):
    assert rule in SUPERVISOR_SYSTEM_PROMPT.lower()


def test_the_prompt_stays_within_its_token_budget():
    """The reason this file exists. One turn's four LLM calls have to fit
    inside an 8,000-token minute; this prompt is sent on every turn and
    was 10,801 characters, roughly a quarter of all input in a turn.

    The ceiling is deliberately loose enough for real edits and tight
    enough that the old version fails it."""
    rendered = build_supervisor_system_prompt(DOMAIN_DEFINITION)
    assert len(rendered) < 8_000, (
        f"classification prompt is {len(rendered)} chars; it is sent on "
        "every turn and competes with the answer prompt for the same "
        "per-minute token budget"
    )


# ---------------------------------------------------------------------------
# Live golden set — opt-in
# ---------------------------------------------------------------------------

#: (message, history, expected category, expected intent). Chosen for
#: discrimination rather than coverage: each one is a case where a weaker
#: prompt plausibly gets it wrong.
GOLDEN: tuple[tuple[str, list, RequestCategory, Intent], ...] = (
    ("Hi there", [], RequestCategory.GREETING, Intent.UNKNOWN),
    (
        "hi, what services do you offer?",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.KNOWLEDGE_QUERY,
    ),
    (
        "What is MVP development?",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.KNOWLEDGE_QUERY,
    ),
    (
        "Who works at Alpinist Studios?",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.KNOWLEDGE_QUERY,
    ),
    (
        "I want to speak to a human",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.CREATE_TICKET,
    ),
    (
        "please open a support ticket for me",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.CREATE_TICKET,
    ),
    (
        "what's the status of my ticket?",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.CHECK_TICKET_STATUS,
    ),
    (
        "has my issue been resolved yet?",
        [],
        RequestCategory.DOMAIN_REQUEST,
        Intent.CHECK_TICKET_STATUS,
    ),
    (
        "How's the weather in Paris?",
        [],
        RequestCategory.OUT_OF_SCOPE,
        Intent.UNKNOWN,
    ),
    (
        "write me a python quicksort",
        [],
        RequestCategory.OUT_OF_SCOPE,
        Intent.UNKNOWN,
    ),
    ("ticket", [], RequestCategory.DOMAIN_REQUEST, Intent.UNKNOWN),
)


@pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set"
)
@pytest.mark.parametrize("message,history,category,intent", GOLDEN)
def test_live_classification_golden_set(message, history, category, intent):
    from agents.supervisor.llm_client import build_llm_client

    client = build_llm_client("groq")
    raw = client.classify(
        system_prompt=build_supervisor_system_prompt(DOMAIN_DEFINITION),
        user_message=message,
        conversation_history=history,
    )
    result = parse_llm_response(raw)

    assert result.request_category is category, f"{message!r} -> {raw}"
    assert result.intent is intent, f"{message!r} -> {raw}"

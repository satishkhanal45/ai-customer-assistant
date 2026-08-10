"""Tests for the canonical cross-agent contracts (``agents/contracts.py``).

These cover the single source of truth every agent boundary shares:
``DownstreamResult`` (the supervisor's hand-off from downstream agents)
and ``ConversationTurn`` / ``flatten_history`` (conversation history
rendering). They test the Pydantic validation and the pure helpers only —
no graph wiring, no network.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.contracts import (
    ConversationTurn,
    DownstreamResult,
    DownstreamStatus,
    flatten_history,
)


# ---------------------------------------------------------------------------
# DownstreamResult
# ---------------------------------------------------------------------------

def test_downstream_result_defaults():
    result = DownstreamResult(
        status=DownstreamStatus.GROUNDED,
        response="This is the answer.",
    )
    assert result.customer_wants_escalation is False
    assert result.schema_version == 1
    assert result.agent_name is None
    assert result.latency_ms is None
    assert result.citations == ()
    assert result.confidence is None


def test_downstream_result_observability_fields():
    result = DownstreamResult(
        status=DownstreamStatus.UNGROUNDED,
        response="No answer yet.",
        customer_wants_escalation=True,
        agent_name="ticket_agent",
        latency_ms=12.5,
        citations=("src/guide.md",),
        confidence=0.42,
    )
    assert result.agent_name == "ticket_agent"
    assert result.latency_ms == 12.5
    assert result.citations == ("src/guide.md",)
    assert result.confidence == 0.42


def test_downstream_result_requires_status_and_response():
    with pytest.raises(ValidationError):
        DownstreamResult()


def test_downstream_result_rejects_invalid_status():
    with pytest.raises(ValidationError):
        DownstreamResult(status="BOGUS", response="nope")


def test_downstream_result_defensive_default_for_unknown_status():
    """A v1 dict with an unknown status (e.g. NOT_IMPLEMENTED from the
    placeholder node) must not leak past the boundary as a Pydantic
    enum — the placeholder's raw dict is consumed by the routing
    decision, and only typed DownstreamResults cross real boundaries.
    """
    result = DownstreamResult(
        status=DownstreamStatus.ERROR, response="fallback"
    )
    assert result.status is DownstreamStatus.ERROR


# ---------------------------------------------------------------------------
# ConversationTurn / flatten_history
# ---------------------------------------------------------------------------

def test_conversation_turn_discourages_agent_roles():
    """role is strictly 'user' | 'assistant'."""
    with pytest.raises(ValidationError):
        ConversationTurn(role="model", content="hi")


def test_flatten_history_renders_user_and_assistant():
    history = [
        ConversationTurn(role="user", content="What is refund policy?"),
        ConversationTurn(role="assistant", content="Here it is."),
        ConversationTurn(role="user", content="Thanks"),
    ]
    flattened = flatten_history(history)
    assert flattened == (
        "User: What is refund policy?",
        "Assistant: Here it is.",
        "User: Thanks",
    )


def test_flatten_history_empty_input_returns_empty_tuple():
    assert flatten_history([]) == ()
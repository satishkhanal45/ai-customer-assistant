"""Tests for the Supervisor agent.

Three layers, tested separately per the immutable/pure-function design:

1. classification.parse_llm_response — pure, no I/O.
2. routing.decide_route / decide_post_downstream — pure, no I/O.
3. graph.build_supervisor_graph — wiring, exercised with a FakeClient so no
   network call happens in the default test run.

A live Gemini call is included as an opt-in integration test, skipped
automatically unless GEMINI_API_KEY is set in the environment.
"""
from __future__ import annotations

import json
import os

import pytest

from ai_customer_assistant.agents.supervisor.classification import parse_llm_response
from ai_customer_assistant.agents.supervisor.graph import build_supervisor_graph
from ai_customer_assistant.agents.supervisor.routing import decide_post_downstream, decide_route
from ai_customer_assistant.agents.supervisor.schema import (
    Intent,
    NextAgent,
    RequestCategory,
    TicketType,
)


class FakeClient:
    """Test double for SupervisorLLMClient — returns a fixed payload."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def classify(self, system_prompt, user_message, conversation_history) -> str:
        return json.dumps(self.payload)


# ---------------------------------------------------------------------------
# classification.parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_llm_response_valid_payload():
    raw = json.dumps(
        {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 0.9,
            "intent": "CREATE_TICKET",
            "intent_confidence": 0.95,
            "clarification_question": None,
        }
    )
    result = parse_llm_response(raw)
    assert result.request_category is RequestCategory.DOMAIN_REQUEST
    assert result.intent is Intent.CREATE_TICKET
    assert result.intent_confidence == 0.95


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "",
        "[]",
        json.dumps({"request_category": "NOT_A_REAL_CATEGORY"}),
    ],
)
def test_parse_llm_response_malformed_input_is_total(raw):
    """Malformed input never raises — it degrades to the safe fallback."""
    result = parse_llm_response(raw)
    assert result.request_category is RequestCategory.OUT_OF_SCOPE
    assert result.intent is Intent.UNKNOWN
    assert result.intent_confidence == 0.0


def test_parse_llm_response_clamps_out_of_range_confidence():
    raw = json.dumps(
        {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 5.0,
            "intent": "KNOWLEDGE_QUERY",
            "intent_confidence": -1.0,
            "clarification_question": None,
        }
    )
    result = parse_llm_response(raw)
    assert result.domain_confidence == 1.0
    assert result.intent_confidence == 0.0


# ---------------------------------------------------------------------------
# routing.decide_route
# ---------------------------------------------------------------------------

def _classification(**overrides):
    base = dict(
        request_category=RequestCategory.DOMAIN_REQUEST,
        domain_confidence=0.9,
        intent=Intent.UNKNOWN,
        intent_confidence=0.0,
        clarification_question=None,
    )
    base.update(overrides)
    from ai_customer_assistant.agents.supervisor.classification import Classification

    return Classification(**base)


def test_greeting_terminates_with_direct_response():
    classification = _classification(request_category=RequestCategory.GREETING)
    decision = decide_route(classification, prior_attempts=0)
    assert decision.next_agent is NextAgent.NONE
    assert decision.clarification_required is False
    assert decision.final_response is not None


def test_out_of_scope_terminates_with_decline():
    classification = _classification(request_category=RequestCategory.OUT_OF_SCOPE)
    decision = decide_route(classification, prior_attempts=0)
    assert decision.next_agent is NextAgent.NONE
    assert decision.final_response is not None


def test_high_confidence_knowledge_query_routes_to_knowledge_agent():
    classification = _classification(intent=Intent.KNOWLEDGE_QUERY, intent_confidence=0.95)
    decision = decide_route(classification, prior_attempts=0)
    assert decision.next_agent is NextAgent.KNOWLEDGE_AGENT
    assert decision.clarification_required is False
    assert decision.ticket_type is None


def test_high_confidence_create_ticket_routes_to_ticket_agent():
    classification = _classification(intent=Intent.CREATE_TICKET, intent_confidence=0.97)
    decision = decide_route(classification, prior_attempts=0)
    assert decision.next_agent is NextAgent.TICKET_AGENT
    assert decision.ticket_type is TicketType.STANDARD


def test_medium_confidence_forces_clarification():
    classification = _classification(intent=Intent.CREATE_TICKET, intent_confidence=0.55)
    decision = decide_route(classification, prior_attempts=0)
    assert decision.clarification_required is True
    assert decision.next_agent is NextAgent.NONE
    assert decision.clarification_attempts == 1


def test_unknown_intent_clarifies_before_exhausting_attempts():
    classification = _classification(intent=Intent.UNKNOWN)
    decision = decide_route(classification, prior_attempts=1)
    assert decision.clarification_required is True
    assert decision.clarification_attempts == 2
    assert decision.next_agent is NextAgent.NONE


def test_unknown_intent_escalates_after_max_attempts():
    classification = _classification(intent=Intent.UNKNOWN)
    decision = decide_route(classification, prior_attempts=2)  # 3rd attempt
    assert decision.next_agent is NextAgent.TICKET_AGENT
    assert decision.ticket_type is TicketType.ESCALATION
    assert decision.clarification_required is False


# ---------------------------------------------------------------------------
# routing.decide_post_downstream
# ---------------------------------------------------------------------------

def test_post_downstream_finalizes_normal_result():
    decision = decide_post_downstream({"status": "OK", "response": "Here you go."})
    assert decision.next_agent is NextAgent.NONE
    assert decision.final_response == "Here you go."


def test_post_downstream_escalates_on_ungrounded_confirmed():
    decision = decide_post_downstream(
        {"status": "UNGROUNDED", "customer_wants_escalation": True}
    )
    assert decision.next_agent is NextAgent.TICKET_AGENT
    assert decision.ticket_type is TicketType.ESCALATION


def test_post_downstream_does_not_escalate_without_confirmation():
    decision = decide_post_downstream(
        {"status": "UNGROUNDED", "customer_wants_escalation": False, "response": "Sorry, not sure."}
    )
    assert decision.next_agent is NextAgent.NONE
    assert decision.final_response == "Sorry, not sure."


# ---------------------------------------------------------------------------
# Full graph wiring (FakeClient — no network)
# ---------------------------------------------------------------------------

def _run(payload: dict, attempts: int = 0) -> dict:
    graph = build_supervisor_graph(llm_client=FakeClient(payload))
    return graph.invoke(
        {"user_message": "irrelevant for FakeClient", "conversation_history": [], "clarification_attempts": attempts}
    )


def test_graph_greeting_end_to_end():
    result = _run({"request_category": "GREETING", "domain_confidence": 0.99, "intent": "UNKNOWN", "intent_confidence": 0.0, "clarification_question": None})
    assert result["final_response"] == "Hello! How can I help you today?"


def test_graph_unknown_exhausted_reaches_ticket_agent():
    payload = {"request_category": "DOMAIN_REQUEST", "domain_confidence": 0.8, "intent": "UNKNOWN", "intent_confidence": 0.0, "clarification_question": None}
    result = _run(payload, attempts=2)
    # Ticket Agent placeholder ran and assemble_response_node finalized it.
    assert result["final_response"] == "Ticket Agent is not yet implemented."
    assert result["clarification_attempts"] == 3


# ---------------------------------------------------------------------------
# Opt-in live integration test — real Groq call, real network.
# Run with: GROQ_API_KEY=... pytest tests/test_supervisor.py -k groq
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set")
def test_groq_live_classifies_a_greeting():
    from ai_customer_assistant.agents.supervisor.llm_client import build_llm_client

    graph = build_supervisor_graph(llm_client=build_llm_client("groq"))
    result = graph.invoke(
        {"user_message": "Hi there!", "conversation_history": [], "clarification_attempts": 0}
    )
    assert result["request_category"] is RequestCategory.GREETING
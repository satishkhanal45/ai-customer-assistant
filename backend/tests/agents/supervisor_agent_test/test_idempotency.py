"""Tests for ticket-ticket-creation idempotency (agents_integration_plan_new.md §2.3 / §4.4).

Two layers, matching how the feature is surfaced:
  - the ``TicketStore`` alone (the pure persistence boundary): the same
    idempotency key at create_ticket time returns the *existing* Ticket and
    never books a second row — this is the mechanism the "duplicate resume"
    guarantee is built on;
  - the graph end-to-end: two resumes of the same ticket-creation flow with
    the same client ``request_id`` (the idempotency key) and the same email
    produce exactly one persisted ticket row and both reads report the same
    ticket id.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agents.supervisor.graph import build_supervisor_graph
from agents.supervisor.routing import decide_route
from agents.supervisor.schema import (
    Intent,
    NextAgent,
    RequestCategory,
)
from agents.ticket_agent.store import TicketStore
from agents.ticket_agent.types import PendingTicket


def _stub_client(payload: dict):
    import json

    class _Client:
        def __init__(self):
            self.payload = payload

        def classify(self, system_prompt, user_message, conversation_history) -> str:
            return json.dumps(self.payload)

    return _Client()


def _classification(**overrides):
    from agents.supervisor.classification import Classification

    base = dict(
        request_category=RequestCategory.DOMAIN_REQUEST,
        domain_confidence=0.9,
        intent=Intent.CREATE_TICKET,
        intent_confidence=0.95,
        clarification_question=None,
    )
    base.update(overrides)
    return Classification(**base)


# ---------------------------------------------------------------------------
# TicketStore-level idempotency
# ---------------------------------------------------------------------------

class TestTicketStoreIdempotency:
    def test_same_key_returns_existing_ticket(self) -> None:
        store = TicketStore()
        pending = PendingTicket(query="Refund question")
        first = store.create_ticket(
            pending, "customer@example.com", idempotency_key="request:req-1"
        )
        second = store.create_ticket(
            pending, "customer@example.com", idempotency_key="request:req-1"
        )
        assert second.ticket_id == first.ticket_id
        assert len(store.rows) == 1
        assert store.rows[0] is first

    def test_distinct_keys_create_distinct_rows(self) -> None:
        store = TicketStore()
        pending = PendingTicket(query="Refund question")
        store.create_ticket(pending, "a@example.com", idempotency_key="request:req-1")
        store.create_ticket(pending, "b@example.com", idempotency_key="request:req-2")
        assert len(store.rows) == 2

    def test_without_key_always_creates_fresh_row(self) -> None:
        store = TicketStore()
        pending = PendingTicket(query="Refund question")
        first = store.create_ticket(pending, "a@example.com")
        second = store.create_ticket(pending, "a@example.com")
        assert first.ticket_id != second.ticket_id
        assert len(store.rows) == 2


class TestIdempotencyKeyDerivation:
    def test_request_id_priority(self) -> None:
        from agents.supervisor.agents_wiring import _idempotency_key

        key = _idempotency_key({"request_id": "req-9", "thread_id": "t"})
        assert key == "request:req-9"

    def test_server_derived_uses_thread_and_sequence(self) -> None:
        from agents.supervisor.agents_wiring import _idempotency_key

        store = TicketStore()
        assert _idempotency_key({"thread_id": "t"}, store=store) == "t:0"
        pending = PendingTicket(query="q")
        store.create_ticket(pending, "a@example.com", idempotency_key="t:0")
        assert _idempotency_key({"thread_id": "t"}, store=store) == "t:1"

    def test_fake_without_sequence_falls_back_to_thread(self) -> None:
        from agents.supervisor.agents_wiring import _idempotency_key

        assert _idempotency_key({"thread_id": "t"}, store=object()) == "t"


# ---------------------------------------------------------------------------
# Graph-level: two resumes with the same request_id + email -> one row
# ---------------------------------------------------------------------------

def test_two_resumes_same_request_id_create_one_row():
    store = TicketStore()
    payload = {
        "request_category": "DOMAIN_REQUEST",
        "domain_confidence": 0.9,
        "intent": "CREATE_TICKET",
        "intent_confidence": 0.97,
        "clarification_question": None,
    }
    graph = build_supervisor_graph(
        llm_client=_stub_client(payload),
        ticket_ops=store,
        checkpointer=MemorySaver(),
    )
    config = {
        "configurable": {"thread_id": "idem-thread-1", "request_id": "req-dup"}
    }
    opened = graph.invoke(
        {
            "user_message": "Please create a ticket for my refund",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    assert "__interrupt__" in opened

    resumed1 = graph.invoke(
        Command(resume="customer@example.com"), config=config
    )
    resumed2 = graph.invoke(
        Command(resume="customer@example.com"), config=config
    )

    assert len(store.rows) == 1
    assert resumed1["final_response"] == resumed2["final_response"]


# ---------------------------------------------------------------------------
# CHECK_TICKET_STATUS -> clarification response (scoped out of MVP)
# ---------------------------------------------------------------------------

class TestCheckTicketStatusRouting:
    def test_high_confidence_status_check_answers_directly(self) -> None:
        decision = decide_route(
            _classification(
                request_category=RequestCategory.DOMAIN_REQUEST,
                intent=Intent.CHECK_TICKET_STATUS,
                intent_confidence=0.97,
            ),
            prior_attempts=0,
        )
        assert decision.next_agent is NextAgent.NONE
        assert decision.clarification_required is False
        assert decision.ticket_type is None
        assert decision.final_response is not None
        assert "status" in decision.final_response.lower()

    def test_low_confidence_status_check_still_answers_directly(self) -> None:
        # Even at low confidence a CHECK_TICKET_STATUS intent is answered in
        # place, before the clarification tier logic would otherwise fire.
        decision = decide_route(
            _classification(
                intent=Intent.CHECK_TICKET_STATUS, intent_confidence=0.2
            ),
            prior_attempts=0,
        )
        assert decision.next_agent is NextAgent.NONE
        assert decision.final_response is not None
        assert "status" in decision.final_response.lower()
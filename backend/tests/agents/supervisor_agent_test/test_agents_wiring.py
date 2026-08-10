"""Tests for the adapter layer between Supervisor and downstream agents.

Phase 1 covers the Knowledge Agent adapter
(``agents_wiring.make_knowledge_agent_node``): state mapping into the
Knowledge graph, the async call under a timeout, and the GroundedResponse-
shaped result / error-marker returned into ``SupervisorState``.

Phase 2 covers the Safety gate (``agents_wiring.make_safety_gate_node``):
the groundedness verdict produced as the canonical ``DownstreamResult``,
the upstream-error short-circuit, and the interrupt()-based escalation
confirmation against a MemorySaver checkpointer.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agents.safety_agent.types import GroundednessResult
from agents.supervisor.agents_wiring import (
    make_knowledge_agent_node,
    make_safety_gate_node,
)
from agents.supervisor.schema import ConversationTurn, SupervisorState
from agents.ticket_agent.types import PendingTicket, Ticket


class FakeKnowledgeGraph:
    """Stand-in for the compiled Knowledge Agent graph (``ainvoke`` only)."""

    def __init__(self, result=None, exc=None, delay=0.0):
        self.result = result
        self.exc = exc
        self.delay = delay
        self.calls: list[dict] = []

    async def ainvoke(self, state: dict) -> dict:
        self.calls.append(state)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeResponse:
    def __init__(self, answer_text, is_grounded, citations=()):
        self.answer_text = answer_text
        self.is_grounded = is_grounded
        self.citations = citations


def _state(**overrides) -> SupervisorState:
    base: dict = {
        "user_message": "What is the refund policy?",
        "conversation_history": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_knowledge_node_maps_state_and_returns_answer():
    fake = FakeKnowledgeGraph(
        result={"response": FakeResponse("Refunds within 30 days.", True)}
    )
    node = make_knowledge_agent_node(fake)

    update = asyncio.run(node(_state()))
    assert update["knowledge_response"]["answer_text"] == "Refunds within 30 days."
    assert update["knowledge_response"]["is_grounded"] is True
    assert update["knowledge_response"]["citations"] == []


def test_knowledge_node_uses_user_message_as_raw_query():
    fake = FakeKnowledgeGraph(
        result={"response": FakeResponse("Policy: 30 days.", True)}
    )
    node = make_knowledge_agent_node(fake)

    asyncio.run(node(_state(user_message="tell me about refunds")))
    assert fake.calls[0]["raw_query"] == "tell me about refunds"


def test_knowledge_node_flattens_history_with_role_labels():
    fake = FakeKnowledgeGraph(
        result={"response": FakeResponse("answer", True)}
    )
    node = make_knowledge_agent_node(fake)

    asyncio.run(
        node(_state(conversation_history=[
            ConversationTurn(role="user", content="hi"),
            ConversationTurn(role="assistant", content="hello"),
        ]))
    )
    assert fake.calls[0]["conversation_history"] == ("User: hi", "Assistant: hello")


def test_knowledge_node_empty_history_maps_to_empty_tuple():
    fake = FakeKnowledgeGraph(result={"response": FakeResponse("answer", True)})
    node = make_knowledge_agent_node(fake)

    asyncio.run(node(_state()))
    assert fake.calls[0]["conversation_history"] == ()


def test_knowledge_node_preserves_citations():
    citations = ("refund-policy.md",)
    fake = FakeKnowledgeGraph(
        result={"response": FakeResponse("answer", True, citations=citations)}
    )
    node = make_knowledge_agent_node(fake)

    update = asyncio.run(node(_state()))
    assert update["knowledge_response"]["citations"] == list(citations)


# ---------------------------------------------------------------------------
# error / timeout path
# ---------------------------------------------------------------------------

def test_knowledge_node_timeout_returns_error_marker():
    fake = FakeKnowledgeGraph(
        result={"response": FakeResponse("too slow", True)}, delay=0.05
    )
    node = make_knowledge_agent_node(fake, timeout_s=0.01)

    update = asyncio.run(node(_state()))
    assert update["knowledge_response"]["error"] == "timeout"
    assert update["knowledge_response"]["answer_text"] == ""
    assert update["knowledge_response"]["is_grounded"] is False


def test_knowledge_node_llm_exception_returns_error_marker():
    fake = FakeKnowledgeGraph(exc=RuntimeError("LLM down"))
    node = make_knowledge_agent_node(fake)

    update = asyncio.run(node(_state()))
    assert update["knowledge_response"]["error"] == "error"
    assert update["knowledge_response"]["answer_text"] == ""


def test_knowledge_node_missing_response_returns_error_marker():
    fake = FakeKnowledgeGraph(result={})
    node = make_knowledge_agent_node(fake)

    update = asyncio.run(node(_state()))
    assert update["knowledge_response"]["error"] == "missing_response"


def test_timeout_flush_is_optional_param():
    """The timeout stays a constructor kwarg so callers (Phase 5
    chat_service) can tune it without changing the node signature."""
    node = make_knowledge_agent_node(FakeKnowledgeGraph(result={}), timeout_s=8)
    assert node is not None


# ---------------------------------------------------------------------------
# graph wiring: knowledge_graph param produces the real node
# ---------------------------------------------------------------------------

def test_build_graph_accepts_knowledge_graph():
    from agents.supervisor.graph import build_supervisor_graph

    graph = build_supervisor_graph(
        llm_client=_StubClient(),
        knowledge_graph=FakeKnowledgeGraph(
            result={"response": FakeResponse("answer", True)}
        ),
    )
    assert graph is not None


def test_build_graph_rejects_both_knowledge_sources():
    """Passing an explicit node callable *and* a knowledge_graph is
    ambiguous — the callable must not silently override a wired graph."""
    from agents.supervisor.graph import build_supervisor_graph

    with pytest.raises(ValueError):
        build_supervisor_graph(
            llm_client=_StubClient(),
            knowledge_agent_node=lambda state: {"knowledge_response": {}},
            knowledge_graph=FakeKnowledgeGraph(
                result={"response": FakeResponse("answer", True)}
            ),
        )


@pytest.mark.asyncio
async def test_wired_knowledge_node_runs_under_ainvoke():
    """Once a real knowledge graph is wired the Supervisor graph is async
    (per plan open question #5); verify the full path populates
    knowledge_response."""
    from agents.supervisor.graph import build_supervisor_graph

    payload = json.dumps(
        {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 0.9,
            "intent": "KNOWLEDGE_QUERY",
            "intent_confidence": 0.95,
            "clarification_question": None,
        }
    )
    graph = build_supervisor_graph(
        llm_client=_StubClient(payload),
        knowledge_graph=FakeKnowledgeGraph(
            result={"response": FakeResponse("answer", False)}
        ),
    )
    result = await graph.ainvoke(
        {"user_message": "What is the refund policy?", "conversation_history": [], "clarification_attempts": 0}
    )
    assert result["knowledge_response"]["answer_text"] == "answer"
    assert result["knowledge_response"]["is_grounded"] is False


# ---------------------------------------------------------------------------
# Phase 2 — safety gate node (``make_safety_gate_node``)
# ---------------------------------------------------------------------------

def _knowledge_routing_payload() -> str:
    return json.dumps(
        {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 0.9,
            "intent": "KNOWLEDGE_QUERY",
            "intent_confidence": 0.95,
            "clarification_question": None,
        }
    )


def _knowledge_node_returning(
    answer_text: str,
    is_grounded: bool,
    *,
    error: str | None = None,
) -> Callable:
    """Sync knowledge node that just publishes a knowledge_response, letting
    the safety gate be exercised at graph level without a compiled RAG graph."""

    def _node(_state) -> dict:
        knowledge_response = {
            "answer_text": answer_text,
            "is_grounded": is_grounded,
            "citations": [],
        }
        if error is not None:
            knowledge_response["error"] = error
        return {"knowledge_response": knowledge_response}

    return _node


def test_safety_gate_grounded_returns_grounded_result():
    """A grounded verdict maps to report_grounded -> status GROUNDED."""
    seen = []

    def groundedness_check(answer: str):
        seen.append(answer)
        return GroundednessResult(is_grounded=True, confidence_score=1.0)

    node = make_safety_gate_node(groundedness_check=groundedness_check)
    update = node(
        {
            "user_message": "What is the refund policy?",
            "knowledge_response": {
                "answer_text": "Refunds within 30 days.",
                "is_grounded": True,
                "citations": [],
            },
        }
    )
    assert seen == ["Refunds within 30 days."]
    assert update["downstream_result"]["status"] == "GROUNDED"
    assert update["downstream_result"]["response"] == "Refunds within 30 days."
    assert update["downstream_result"]["customer_wants_escalation"] is False


def test_safety_gate_upstream_error_short_circuits():
    """The groundedness check must never run on a failed retrieval."""
    called = False

    def groundedness_check(_: str):
        nonlocal called
        called = True
        return GroundednessResult(is_grounded=True, confidence_score=1.0)

    node = make_safety_gate_node(groundedness_check=groundedness_check)
    update = node(
        {
            "user_message": "What is the refund policy?",
            "knowledge_response": {
                "answer_text": "",
                "is_grounded": False,
                "citations": [],
                "error": "timeout",
            },
        }
    )
    assert called is False
    assert update["downstream_result"]["status"] == "ERROR"
    assert update["downstream_result"]["customer_wants_escalation"] is False


def test_safety_gate_grounded_uses_knowledge_hint_when_no_check():
    """Default verdict trusts Knowledge's own is_grounded hint."""
    node = make_safety_gate_node()
    update = node(
        {
            "user_message": "What is the refund policy?",
            "knowledge_response": {
                "answer_text": "Refunds within 30 days.",
                "is_grounded": True,
                "citations": [],
            },
        }
    )
    assert update["downstream_result"]["status"] == "GROUNDED"


def _build_safety_graph():
    """Graph wired with a sync knowledge node that always returns an
    ungrounded answer plus the real safety gate, on a MemorySaver
    checkpointer (required for the interrupt() flow)."""
    from agents.supervisor.graph import build_supervisor_graph

    return build_supervisor_graph(
        llm_client=_StubClient(_knowledge_routing_payload()),
        knowledge_agent_node=_knowledge_node_returning(
            "answer", is_grounded=False
        ),
        safety_gate_node=make_safety_gate_node(
            groundedness_check=lambda answer: GroundednessResult(
                is_grounded=False, confidence_score=0.0
            )
        ),
        checkpointer=MemorySaver(),
    )


def test_safety_gate_ungraded_pauses_with_escalation_confirmation():
    """First ungrounded turn: interrupt() with an escalation-confirmation
    payload; no downstream_result is produced until the resume."""
    from agents.supervisor.graph import build_supervisor_graph

    graph = _build_safety_graph()

    config = {"configurable": {"thread_id": "safety-decline"}}
    first = graph.invoke(
        {
            "user_message": "What is the refund policy?",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    assert "__interrupt__" in first
    (interrupt_payload,) = first["__interrupt__"]
    assert interrupt_payload.value["type"] == "escalation-confirmation"
    assert "downstream_result" not in first

    declined = graph.invoke(Command(resume=False), config=config)
    assert declined["downstream_result"]["status"] == "UNGROUNDED"
    assert declined["downstream_result"]["customer_wants_escalation"] is False
    assert declined["final_response"] == declined["downstream_result"]["response"]


def test_safety_gate_ungraded_after_confirm_escalates():
    """Customer confirms -> the ESCALATE branch of the post-downstream edge
    fires, the Ticket Agent opens a ticket and interrupt()s for the email;
    on resume it produces a real Ticket + non-null final_response."""
    from agents.supervisor.graph import build_supervisor_graph

    fake_ticket_ops = FakeTicketOps()
    graph = build_supervisor_graph(
        llm_client=_StubClient(_knowledge_routing_payload()),
        knowledge_agent_node=_knowledge_node_returning(
            "answer", is_grounded=False
        ),
        safety_gate_node=make_safety_gate_node(
            groundedness_check=lambda answer: GroundednessResult(
                is_grounded=False, confidence_score=0.0
            )
        ),
        ticket_ops=fake_ticket_ops,
        checkpointer=MemorySaver(),
    )

    config = {"configurable": {"thread_id": "safety-confirm"}}
    graph.invoke(
        {
            "user_message": "What is the refund policy?",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    email_interrupt = graph.invoke(Command(resume=True), config=config)
    assert "__interrupt__" in email_interrupt
    (email_payload,) = email_interrupt["__interrupt__"]
    assert email_payload.value["type"] == "email-collection"

    ticket = graph.invoke(
        Command(resume="customer@example.com"), config=config
    )
    assert ticket["downstream_result"]["status"] == "GROUNDED"
    assert ticket["final_response"] is not None
    assert "customer@example.com" in ticket["final_response"]
    assert "What is the refund policy?" in fake_ticket_ops.called_with_query
    assert len(fake_ticket_ops.created) == 1
    assert fake_ticket_ops.created[0].email == "customer@example.com"


def test_safety_gate_ungraded_decline_takes_finalize_branch():
    """Customer declines -> FINALIZE branch: assemble_response finalizes the
    fallback text into final_response, never reaching the Ticket Agent."""
    from agents.supervisor.graph import build_supervisor_graph

    fake_ticket_ops = FakeTicketOps()
    graph = build_supervisor_graph(
        llm_client=_StubClient(_knowledge_routing_payload()),
        knowledge_agent_node=_knowledge_node_returning(
            "answer", is_grounded=False
        ),
        safety_gate_node=make_safety_gate_node(
            groundedness_check=lambda answer: GroundednessResult(
                is_grounded=False, confidence_score=0.0
            )
        ),
        ticket_ops=fake_ticket_ops,
        checkpointer=MemorySaver(),
    )

    config = {"configurable": {"thread_id": "safety-finalize"}}
    graph.invoke(
        {
            "user_message": "What is the refund policy?",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    declined = graph.invoke(Command(resume=False), config=config)
    assert declined["downstream_result"]["status"] == "UNGROUNDED"
    assert declined["downstream_result"]["customer_wants_escalation"] is False
    assert declined["final_response"] is not None
    assert declined["final_response"] == declined["downstream_result"]["response"]
    assert fake_ticket_ops.calls == []
    assert fake_ticket_ops.created == []


class FakeTicketOps:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.created: list = []
        self.called_with_query: list[str] = []
        self.idempotency_keys: list[str] = []

    def call(self, query: str):
        self.calls.append(("call", query))
        self.called_with_query.append(query)
        return PendingTicket(query=query)

    def create_ticket(self, pending, email: str, idempotency_key=None):  # type: ignore[no-untyped-def]
        ticket = Ticket(
            ticket_id="ticket-1", email=email, query=pending.query
        )
        self.created.append(ticket)
        self.idempotency_keys.append(idempotency_key)
        return ticket


class _StubClient:
    def __init__(self, payload: str = "{}") -> None:
        self.payload = payload

    def classify(self, system_prompt, user_message, conversation_history) -> str:
        return self.payload
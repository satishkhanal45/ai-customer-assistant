"""Tests for the adapter layer between Supervisor and downstream agents.

Covers the Knowledge Agent adapter
(``agents_wiring.make_knowledge_agent_node``): state mapping into the
Knowledge graph, the async call under a timeout, and the downstream_result
it returns into ``SupervisorState``.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agents.supervisor.agents_wiring import make_knowledge_agent_node
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
    assert update["downstream_result"]["status"] == "GROUNDED"
    assert update["downstream_result"]["response"] == "Refunds within 30 days."
    assert update["downstream_result"]["citations"] == []


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
    assert update["downstream_result"]["citations"] == list(citations)


# ---------------------------------------------------------------------------
# error / timeout path
# ---------------------------------------------------------------------------

def test_knowledge_node_timeout_returns_error_result():
    fake = FakeKnowledgeGraph(
        result={"response": FakeResponse("too slow", True)}, delay=0.05
    )
    node = make_knowledge_agent_node(fake, timeout_s=0.01)

    update = asyncio.run(node(_state()))
    assert update["downstream_result"]["status"] == "ERROR"
    assert update["downstream_result"]["error"] == "timeout"
    assert update["downstream_result"]["response"]


def test_knowledge_node_llm_exception_returns_error_result():
    fake = FakeKnowledgeGraph(exc=RuntimeError("LLM down"))
    node = make_knowledge_agent_node(fake)

    update = asyncio.run(node(_state()))
    assert update["downstream_result"]["status"] == "ERROR"
    assert update["downstream_result"]["error"] == "error"


def test_knowledge_node_missing_response_returns_error_result():
    fake = FakeKnowledgeGraph(result={})
    node = make_knowledge_agent_node(fake)

    update = asyncio.run(node(_state()))
    assert update["downstream_result"]["status"] == "ERROR"
    assert update["downstream_result"]["error"] == "missing_response"


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
    downstream_result."""
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
    assert result["downstream_result"]["status"] == "GROUNDED"
    assert result["downstream_result"]["response"] == "answer"
    assert result["final_response"] == "answer"


# ---------------------------------------------------------------------------
# Ticket Agent flow
# ---------------------------------------------------------------------------

def test_ticket_agent_flow_collects_email_then_creates_ticket():
    """The Ticket Agent's two-step flow: open the ticket (interrupt for the
    email), then resume with the email to create the Ticket and finalize."""
    from langgraph.types import Command

    from agents.supervisor.graph import build_supervisor_graph

    payload = json.dumps(
        {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 0.9,
            "intent": "CREATE_TICKET",
            "intent_confidence": 0.97,
            "clarification_question": None,
        }
    )
    fake_ticket_ops = FakeTicketOps()
    graph = build_supervisor_graph(
        llm_client=_StubClient(payload),
        ticket_ops=fake_ticket_ops,
        checkpointer=MemorySaver(),
    )

    config = {"configurable": {"thread_id": "ticket-flow"}}
    first = graph.invoke(
        {
            "user_message": "I need a refund",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    assert "__interrupt__" in first
    (email_payload,) = first["__interrupt__"]
    assert email_payload.value["type"] == "email-collection"

    ticket = graph.invoke(
        Command(resume="customer@example.com"), config=config
    )
    assert ticket["downstream_result"]["status"] == "GROUNDED"
    assert ticket["final_response"] is not None
    assert "customer@example.com" in ticket["final_response"]
    assert "I need a refund" in fake_ticket_ops.called_with_query
    assert len(fake_ticket_ops.created) == 1


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
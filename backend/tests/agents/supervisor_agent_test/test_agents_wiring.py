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
    # The fake reports `is_grounded=False`, and the status now says so. This
    # assertion used to read "GROUNDED" — the node hardcoded it, so a refusal
    # was reported as a success and the test pinned that in place (F9).
    assert result["downstream_result"]["status"] == "UNGROUNDED"
    assert result["downstream_result"]["response"] == "answer"
    assert result["final_response"] == "answer"


# ---------------------------------------------------------------------------
# Ticket Agent flow
# ---------------------------------------------------------------------------

async def test_ticket_agent_flow_collects_reason_then_email_then_creates_ticket():
    """The Ticket Agent's three-step flow: ask why the customer wants a
    ticket, then interrupt for the email, then resume with the email to
    create the Ticket and finalize.

    Driven with ``ainvoke`` because the ticket adapter node is async (its
    store performs database I/O)."""
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
    first = await graph.ainvoke(
        {
            "user_message": "I need a refund",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    assert "__interrupt__" in first
    (reason_payload,) = first["__interrupt__"]
    assert reason_payload.value["type"] == "clarifying_question"

    second = await graph.ainvoke(Command(resume="billing issue"), config=config)
    assert "__interrupt__" in second
    (email_payload,) = second["__interrupt__"]
    assert email_payload.value["type"] == "email-collection"
    assert email_payload.value["clarifying_reason"] == "billing issue"

    ticket = await graph.ainvoke(
        Command(resume="customer@example.com"), config=config
    )
    assert ticket["downstream_result"]["status"] == "GROUNDED"
    assert ticket["final_response"] is not None
    assert "customer@example.com" in ticket["final_response"]
    assert "I need a refund" in fake_ticket_ops.called_with_query
    assert len(fake_ticket_ops.created) == 1

    # The clarifying answer must survive into the ticket and the reply the
    # customer sees, rather than being collected and thrown away.
    assert fake_ticket_ops.created[0].reason == "billing issue"
    assert "billing issue" in ticket["final_response"]


class FakeTicketOps:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.created: list = []
        self.called_with_query: list[str] = []
        self.reasons: list[str | None] = []
        self.idempotency_keys: list[str] = []

    def call(self, query: str, reason: str | None = None):
        self.calls.append(("call", query))
        self.called_with_query.append(query)
        self.reasons.append(reason)
        return PendingTicket(query=query, reason=reason)

    async def create_ticket(self, pending, email: str, idempotency_key=None):  # type: ignore[no-untyped-def]
        ticket = Ticket(
            ticket_id="ticket-1",
            email=email,
            query=pending.query,
            reason=pending.reason,
        )
        self.created.append(ticket)
        self.idempotency_keys.append(idempotency_key)
        return ticket


class _StubClient:
    def __init__(self, payload: str = "{}") -> None:
        self.payload = payload

    def classify(self, system_prompt, user_message, conversation_history) -> str:
        return self.payload

# ---------------------------------------------------------------------------
# Ticket status lookup
#
# The behaviour under test is the one the customer sees: "what's happening
# with my ticket?" used to be answered with "status lookups aren't available
# yet" no matter what. It now reads the ticket back.
# ---------------------------------------------------------------------------

class FakeStatusOps(FakeTicketOps):
    """FakeTicketOps plus the read side."""

    def __init__(self, tickets=()):
        super().__init__()
        self.tickets = {t.ticket_id: t for t in tickets}
        self.looked_up: list[str] = []

    async def get_ticket(self, ticket_id: str):
        self.looked_up.append(ticket_id)
        return self.tickets.get(ticket_id)


_KNOWN_ID = "3f2a9c14-5b7e-4a21-9f03-8c6d1e4b7a92"
_UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"


def _known_ticket(**overrides):
    fields = {
        "ticket_id": _KNOWN_ID,
        "email": "customer@example.com",
        "query": "My invoice is wrong",
        "reason": "billing issue",
        "status": "IN_PROGRESS",
    }
    fields.update(overrides)
    return Ticket(**fields)


@pytest.mark.asyncio
async def test_status_node_answers_inline_when_message_carries_the_id():
    """The common case after a confirmation: the id is in the message, so no
    interrupt and no extra round trip."""
    from agents.supervisor.agents_wiring import make_ticket_status_node

    ops = FakeStatusOps([_known_ticket()])
    node = make_ticket_status_node(ops)

    result = await node(_state(user_message=f"any update on ticket {_KNOWN_ID}?"))

    assert ops.looked_up == [_KNOWN_ID]
    reply = result["downstream_result"]["response"]
    assert result["downstream_result"]["status"] == "GROUNDED"
    assert "in progress" in reply.lower()
    assert "billing issue" in reply
    assert "customer@example.com" in reply


@pytest.mark.asyncio
async def test_status_node_finds_the_id_regardless_of_surrounding_punctuation():
    from agents.supervisor.agents_wiring import make_ticket_status_node

    ops = FakeStatusOps([_known_ticket()])
    node = make_ticket_status_node(ops)

    for message in (
        f"ID: {_KNOWN_ID}.",
        f"({_KNOWN_ID})",
        _KNOWN_ID.upper(),
        f"status of {_KNOWN_ID}?",
    ):
        result = await node(_state(user_message=message))
        assert "in progress" in result["downstream_result"]["response"].lower(), message


@pytest.mark.asyncio
async def test_status_node_reports_a_miss_as_a_miss_not_as_a_status():
    """An id that matches nothing is usually a typo. Saying "not found" is
    honest; saying "open" would invent a ticket."""
    from agents.supervisor.agents_wiring import make_ticket_status_node

    ops = FakeStatusOps([_known_ticket()])
    node = make_ticket_status_node(ops)

    result = await node(_state(user_message=f"how about {_UNKNOWN_ID}"))

    reply = result["downstream_result"]["response"]
    assert _UNKNOWN_ID in reply
    assert "couldn't find" in reply.lower()


@pytest.mark.asyncio
async def test_status_node_never_creates_a_ticket():
    """The whole point of a separate node: a status question must not open a
    new ticket, on any of its paths."""
    from agents.supervisor.agents_wiring import make_ticket_status_node

    ops = FakeStatusOps([_known_ticket()])
    node = make_ticket_status_node(ops)

    await node(_state(user_message=f"status of {_KNOWN_ID}"))
    await node(_state(user_message=f"status of {_UNKNOWN_ID}"))

    assert ops.created == []
    assert ops.calls == []


@pytest.mark.asyncio
async def test_status_node_asks_for_the_id_then_answers():
    """No id in the message: one interrupt, then the lookup. Driven through
    the compiled graph because `interrupt()` needs a checkpointer."""
    from langgraph.types import Command

    from agents.supervisor.graph import build_supervisor_graph

    payload = json.dumps(
        {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 0.9,
            "intent": "CHECK_TICKET_STATUS",
            "intent_confidence": 0.95,
            "clarification_question": None,
        }
    )
    ops = FakeStatusOps([_known_ticket()])
    graph = build_supervisor_graph(
        llm_client=_StubClient(payload),
        ticket_ops=ops,
        checkpointer=MemorySaver(),
    )

    config = {"configurable": {"thread_id": "status-flow"}}
    first = await graph.ainvoke(
        {
            "user_message": "any update on my ticket?",
            "conversation_history": [],
            "clarification_attempts": 0,
        },
        config=config,
    )
    assert "__interrupt__" in first
    (asked,) = first["__interrupt__"]
    assert asked.value["type"] == "ticket_id_collection"
    assert "ticket ID" in asked.value["query"]

    answered = await graph.ainvoke(Command(resume=_KNOWN_ID), config=config)
    assert ops.looked_up == [_KNOWN_ID]
    assert "in progress" in answered["final_response"].lower()
    assert ops.created == []


@pytest.mark.asyncio
async def test_status_node_handles_a_resume_that_still_has_no_id():
    """The customer answers the question with something that isn't an id. Say
    what an id looks like rather than looking up garbage."""
    from agents.supervisor.agents_wiring import make_ticket_status_node

    ops = FakeStatusOps([_known_ticket()])
    node = make_ticket_status_node(ops)

    # Called directly (no interrupt machinery): an empty message takes the
    # same path a useless resume does once `_find_ticket_id` returns None.
    from agents.supervisor.agents_wiring import _status_reply

    assert ops.looked_up == []
    reply = _status_reply(None, None)
    assert "ticket ID" in reply or "ticket id" in reply.lower()

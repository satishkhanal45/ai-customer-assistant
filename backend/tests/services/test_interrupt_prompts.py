"""Interrupt-response fidelity tests (Phase 5, §4.5).

``handle_message`` must surface the *live* assistant prompt for whichever
pause the graph produces, not a single generic fallback. Each of the three
pauses returns a distinct, correct string:

  1. Supervisor's clarification question (``clarification_question``);
  2. Safety's escalation-confirmation prompt (the ``interrupt()`` payload's
     ``question`` field — a ``generate_fallback_response`` message);
  3. Ticket's email-collection prompt.

These tests assert the actual payload text for each type, exercising the
real graph paths (stub classifier + a fake downstream shapes), not just
that a non-null string comes back.
"""
from __future__ import annotations

import json

import pytest

from agents.safety_agent.fallback_response import generate_fallback_response
from agents.supervisor.llm_client import StubSupervisorLLMClient
from services.chat_service import build_chat_service


def _stub(payload: dict) -> StubSupervisorLLMClient:
    class _Client(StubSupervisorLLMClient):
        def classify(self, system_prompt, user_message, conversation_history) -> str:
            return json.dumps(payload)

    return _Client()


def _clarification_payload(question: str) -> dict:
    return {
        "request_category": "DOMAIN_REQUEST",
        "domain_confidence": 0.9,
        "intent": "UNKNOWN",
        "intent_confidence": 0.2,
        "clarification_question": question,
    }


class _FakeResponse:
    def __init__(self, answer_text: str, is_grounded: bool, citations=()):
        self.answer_text = answer_text
        self.is_grounded = is_grounded
        self.citations = citations


class _FakeKnowledgeGraph:
    def __init__(self, is_grounded: bool):
        self.is_grounded = is_grounded

    async def ainvoke(self, input_state) -> dict:
        return {
            "response": _FakeResponse(
                "A fabricated answer.", self.is_grounded, ()
            )
        }


class TestClarificationQuestion:
    @pytest.mark.asyncio
    async def test_clarification_returns_its_own_question(self):
        svc = await build_chat_service(
            llm_client=_stub(
                _clarification_payload("Which of these do you need — tickets or help?")
            )
        )
        reply = await svc.handle_message("t-clarify", "hmm")
        assert reply == "Which of these do you need — tickets or help?"

    @pytest.mark.asyncio
    async def test_clarification_distinct_from_other_interrupts(self):
        svc = await build_chat_service(
            llm_client=_stub(_clarification_payload("Should I open a ticket for you?"))
        )
        reply = await svc.handle_message("t-clar-2", "whatever")
        assert reply == "Should I open a ticket for you?"
        assert reply != generate_fallback_response("whatever")
        # and it must not be swallowed by a generic fallback.
        assert reply  # non-empty, LLM-specific
        from services.chat_service import _EMAIL_COLLECTION_QUESTION

        assert reply != _EMAIL_COLLECTION_QUESTION


class TestEscalationConfirmation:
    @pytest.mark.asyncio
    async def test_ungrounded_answer_asks_for_escalation_with_real_fallback(self):
        """A knowledge answer that fails groundedness hits the Safety gate's
        escalation-confirmation interrupt; the returned text is the
        payload's generated fallback question."""
        knowledge_query = {
            "request_category": "DOMAIN_REQUEST",
            "domain_confidence": 0.9,
            "intent": "KNOWLEDGE_QUERY",
            "intent_confidence": 0.97,
            "clarification_question": None,
        }
        # Inject a knowledge graph that returns an UNGROUNDED answer, so the
        # default safety gate fires its escalation-confirmation pause.
        from agents.supervisor.graph import build_supervisor_graph

        svc = await build_chat_service(llm_client=_stub(knowledge_query))
        svc.graph = build_supervisor_graph(
            llm_client=_stub(knowledge_query),
            knowledge_graph=_FakeKnowledgeGraph(is_grounded=False),
            ticket_ops=None,
            checkpointer=svc.graph.checkpointer,
        )
        query = "Does your policy cover accidental damage?"
        reply = await svc.handle_message("t-escal", query)
        assert reply == generate_fallback_response(query)
        assert generate_fallback_response(query) != generate_fallback_response("x")


class TestTicketEmailCollection:
    @pytest.mark.asyncio
    async def test_ticket_open_asks_for_email(self):
        svc = await build_chat_service(llm_client=_stub(_create_ticket_payload()))
        reply = await svc.handle_message("t-email", "Open a ticket please")
        assert "email" in reply.lower()
        from services.chat_service import _EMAIL_COLLECTION_QUESTION

        assert reply == _EMAIL_COLLECTION_QUESTION
        # distinct from the safety escalation text
        assert reply != generate_fallback_response("Open a ticket please")


def _create_ticket_payload() -> dict:
    return {
        "request_category": "DOMAIN_REQUEST",
        "domain_confidence": 0.9,
        "intent": "CREATE_TICKET",
        "intent_confidence": 0.97,
        "clarification_question": None,
    }


def test_interrupt_prompts_are_mutually_distinct():
    from services.chat_service import _EMAIL_COLLECTION_QUESTION

    fallback = generate_fallback_response("q")
    clarification = "Which one do you need?"
    assert len({_EMAIL_COLLECTION_QUESTION, fallback, clarification}) == 3
    assert _EMAIL_COLLECTION_QUESTION != fallback
    assert _EMAIL_COLLECTION_QUESTION != clarification
    assert fallback != clarification
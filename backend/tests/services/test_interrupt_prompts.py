"""Interrupt-response fidelity tests (Phase 5, §4.5).

``handle_message`` must surface the *live* assistant prompt for whichever
pause the graph produces, not a single generic fallback. Each pause returns
a distinct, correct string:

  1. Supervisor's clarification question (``clarification_question``);
  2. Ticket's email-collection prompt.

These tests assert the actual payload text for each type, exercising the
real graph paths (stub classifier + a fake downstream shapes), not just
that a non-null string comes back.
"""
from __future__ import annotations

import json

import pytest

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
    async def test_clarification_distinct_from_email_collection(self):
        svc = await build_chat_service(
            llm_client=_stub(_clarification_payload("Should I open a ticket for you?"))
        )
        reply = await svc.handle_message("t-clar-2", "whatever")
        assert reply == "Should I open a ticket for you?"
        # and it must not be swallowed by a generic fallback.
        assert reply  # non-empty, LLM-specific
        from services.chat_service import _EMAIL_COLLECTION_QUESTION

        assert reply != _EMAIL_COLLECTION_QUESTION


class TestTicketEmailCollection:
    @pytest.mark.asyncio
    async def test_ticket_open_asks_for_email(self):
        svc = await build_chat_service(llm_client=_stub(_create_ticket_payload()))
        reply = await svc.handle_message("t-email", "Open a ticket please")
        assert "email" in reply.lower()
        from services.chat_service import _EMAIL_COLLECTION_QUESTION

        assert reply == _EMAIL_COLLECTION_QUESTION


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

    clarification = "Which one do you need?"
    assert len({_EMAIL_COLLECTION_QUESTION, clarification}) == 2
    assert _EMAIL_COLLECTION_QUESTION != clarification
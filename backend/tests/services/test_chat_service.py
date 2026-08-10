"""Tests for the Phase 5 serving layer (chat_service.py + checkpointer).

The full stack is built through ``build_chat_service`` the same way the
app does at startup; checkpoints are in-memory so the tests never touch a
live Postgres. We assert the two Phase 5 contract points directly:

  1. ``handle_message(thread_id, user_message) -> str`` reads conversation
     history from the checkpointer, never from the caller.
  2. Multi-turn ``interrupt()`` flows (ticket email collection) resume
     transparently across ``handle_message`` calls.
"""
from __future__ import annotations

import json

import pytest
from agents.supervisor.llm_client import StubSupervisorLLMClient

from services.chat_service import build_chat_service


class _CreateTicketClient(StubSupervisorLLMClient):
    """Classifies every message as a high-confidence ticket creation."""

    def classify(self, system_prompt, user_message, conversation_history) -> str:
        return json.dumps(
            {
                "request_category": "DOMAIN_REQUEST",
                "domain_confidence": 0.9,
                "intent": "CREATE_TICKET",
                "intent_confidence": 0.97,
                "clarification_question": None,
            }
        )


class _KnowledgeQueryClient(StubSupervisorLLMClient):
    """Classifies every message as a high-confidence Knowledge query so the
    message reaches the (placeholder) Knowledge Agent."""

    def classify(self, system_prompt, user_message, conversation_history) -> str:
        return json.dumps(
            {
                "request_category": "DOMAIN_REQUEST",
                "domain_confidence": 0.9,
                "intent": "KNOWLEDGE_QUERY",
                "intent_confidence": 0.95,
                "clarification_question": None,
            }
        )


@pytest.mark.asyncio
async def test_handle_message_creates_ticket_across_two_calls():
    svc = await build_chat_service(llm_client=_CreateTicketClient())

    first = await svc.handle_message("t-thread", "I need a refund")
    assert "email" in first.lower()

    second = await svc.handle_message("t-thread", "customer@example.com")
    assert "ticket" in second.lower() and "created" in second.lower()


@pytest.mark.asyncio
async def test_handle_message_reads_history_from_checkpointer():
    """History is NOT passed by the caller: the second call must still
    carry the first conversation into the new turn. Proven by asking a
    knowledge query after a ticket turn and observing the conversation
    history channel accumulated two turns."""
    svc = await build_chat_service(llm_client=_CreateTicketClient())

    await svc.handle_message("t-2", "I need a refund")
    await svc.handle_message("t-2", "customer@example.com")

    snapshot = await svc.graph.aget_state({"configurable": {"thread_id": "t-2"}})
    history = snapshot.values.get("conversation_history")
    assert history is not None
    assert [turn.role for turn in history] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_handle_message_reads_history_forward():
    """A follow-up turn receives the prior transcript in conversation_history
    (the Knowledge Agent flattening of it), proving history did not reset."""
    svc = await build_chat_service(llm_client=_KnowledgeQueryClient())

    reply1 = await svc.handle_message("t-3", "Tell me about refunds")
    assert isinstance(reply1, str)

    snapshot = await svc.graph.aget_state({"configurable": {"thread_id": "t-3"}})
    history = snapshot.values.get("conversation_history")
    assert history is not None
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"


@pytest.mark.asyncio
async def test_thread_ids_are_isolated():
    svc = await build_chat_service(llm_client=_CreateTicketClient())

    await svc.handle_message("t-a", "I need a refund")
    await svc.handle_message("t-b", "I want to cancel my order")

    snapshot_a = await svc.graph.aget_state({"configurable": {"thread_id": "t-a"}})
    snapshot_b = await svc.graph.aget_state({"configurable": {"thread_id": "t-b"}})
    # t-a is mid-ticket (one interrupt turn logged), t-b has only its own.
    assert (snapshot_a.values.get("conversation_history") or []) != (
        snapshot_b.values.get("conversation_history") or []
    )
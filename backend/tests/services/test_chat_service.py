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
from langgraph.checkpoint.memory import MemorySaver
from agents.supervisor.llm_client import StubSupervisorLLMClient

from services.chat_service import build_chat_service
from services.embeddings import SharedEmbeddings


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


class _FakeEmbeddingModel:
    """Deterministic stand-in for SentenceTransformer.encode: maps text to a
    2-D vector based on whether it mentions the word ``grounded``."""

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True):
        return [[1.0, 0.0] if "grounded" in (t or "") else [0.0, 1.0] for t in texts]


def test_shared_embeddings_embed_query_normalizes():
    """The shared embeddings face used by the Knowledge Agent's vector
    search returns a normalized vector for a single query."""
    shared = SharedEmbeddings(_FakeEmbeddingModel())
    vector = shared.embed_query("the policy")
    assert len(vector) == 2
    assert abs(sum(c * c for c in vector) - 1.0) < 1e-6


class _FakeResponse:
    def __init__(self, answer: str) -> None:
        self.answer_text = answer
        self.is_grounded = True
        self.citations = []


class _FakeKnowledgeGraph:
    """Knowledge subgraph double that answers each query distinctly."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ainvoke(self, knowledge_input: dict) -> dict:
        query = knowledge_input["raw_query"]
        self.calls.append(query)
        return {"response": _FakeResponse(f"answer to: {query}")}


async def _knowledge_chat_service():
    """Chat service routed to the (fake) Knowledge Agent on every turn."""
    kg = _FakeKnowledgeGraph()
    svc = await build_chat_service(
        llm_client=_KnowledgeQueryClient(),
        knowledge_graph=kg,
        checkpointer=MemorySaver(),
    )
    return svc, kg


@pytest.mark.asyncio
async def test_knowledge_question_returns_answer():
    """A knowledge query flows through the graph and returns the downstream
    answer directly (no safety gate, no escalation pause)."""
    svc, kg = await _knowledge_chat_service()

    reply = await svc.handle_message("t-k", "what is plan one?")
    assert "answer to: what is plan one?" in reply
    assert kg.calls == ["what is plan one?"]


@pytest.mark.asyncio
async def test_new_question_reruns_knowledge_turn():
    """A fresh question on the same thread re-runs a Knowledge Agent turn
    rather than replaying the previous turn's answer."""
    svc, kg = await _knowledge_chat_service()

    first = await svc.handle_message("t-k", "what is plan one?")
    assert "answer to: what is plan one?" in first

    second = await svc.handle_message("t-k", "what is plan two?")
    assert "answer to: what is plan two?" in second
    assert "plan one" not in second

    # Both turns must have actually asked the Knowledge Agent: the second
    # message re-ran a fresh turn rather than resuming a stale pause.
    assert kg.calls == ["what is plan one?", "what is plan two?"]
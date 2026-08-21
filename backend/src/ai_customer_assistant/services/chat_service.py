"""Serving layer: the single dependency-construction point and the chat
endpoint entry (agents_integration_plan_new.md §4.5 / §5).

Everything a request needs is built once here and reused across requests:

- ``db.checkpointer.build_checkpointer()``: the durable checkpointer
  (Postgres when configured, MemorySaver otherwise).
- ``agents.supervisor.graph.build_supervisor_graph(...)``: the compiled
  Supervisor graph with the ticket store / optional knowledge graph
  injected.
- The Supervisor ``llm_client`` (real provider or the deterministic stub,
  chosen by ``build_llm_client`` from the environment).
- The shared BGE embedding singleton (§4.6): ``build_shared_embeddings``
  constructs exactly one ``SentenceTransformer`` for the process and feeds
  ``embed_query`` to the compiled Knowledge graph's ``vector_search``.

``ChatService.handle_message`` is the only chat surface. Conversation
history is never passed in by the caller: it lives behind the checkpointer
keyed by ``thread_id`` and is read back on every turn (§5). The API only
sends the new ``user_message`` plus the ``thread_id``.

Multi-turn flows via ``interrupt()`` (ticket email collection) are surfaced
transparently:

- When the previous turn paused waiting for input (a pending interrupt),
  the next ``user_message`` resumes the graph via ``Command(resume=...)``.
- When the current turn pauses, ``handle_message`` returns the question
  the assistant needs answered, and the thread stays open in the
  checkpointer for the follow-up.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.contracts import ConversationTurn
from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.graph import build_knowledge_agent_graph
from agents.knowledge.providers import build_knowledge_provider, llm_completions
from agents.supervisor.graph import build_supervisor_graph
from agents.supervisor.llm_client import SupervisorLLMClient, build_llm_client
from agents.ticket_agent.store import TicketStore
from db.checkpointer import build_checkpointer
from services.embeddings import SharedEmbeddings

_EMAIL_COLLECTION_QUESTION = (
    "To create your ticket I first need your email address. "
    "Please reply with it and I'll finish the rest."
)


def _citations_from_result(result: Mapping[str, Any]) -> list[dict]:
    """Pull the ``ChunkProvenance`` list the Knowledge node attached to the
    run (``downstream_result.citations``) into plain serializable dicts."""
    downstream_result = result.get("downstream_result") or {}
    citations = downstream_result.get("citations") or []
    out: list[dict] = []
    for c in citations:
        out.append(
            {
                "source_name": getattr(c, "source_name", None),
                "page": getattr(c, "page", None),
                "version_number": getattr(c, "version_number", None),
                "category_name": getattr(c, "category_name", None),
            }
        )
    return out


class ChatService:
    """Owns the compiled Supervisor graph and the checkpointer-backed
    conversation state for every thread."""

    def __init__(
        self,
        graph: Any,
        llm_client: SupervisorLLMClient,
        embedding_model: Optional[object] = None,
    ) -> None:
        self.graph = graph
        self.llm_client = llm_client
        # The shared BGE instance (Phase 6, §4.6): the same SentenceTransformer
        # the compiled Knowledge graph's `embed_query` was built from, kept here
        # so callers that need the embedding model reuse the same instance —
        # never re-instantiated per request.
        self.embedding_model = embedding_model

    async def handle_message(
        self,
        thread_id: str,
        user_message: str,
        *,
        trace_id: Optional[str] = None,
    ) -> str:
        """Process one user message in ``thread_id`` and return the
        assistant's reply text."""
        reply, _ = await self.handle_message_turn(
            thread_id, user_message, trace_id=trace_id
        )
        return reply

    async def handle_message_turn(
        self,
        thread_id: str,
        user_message: str,
        *,
        trace_id: Optional[str] = None,
    ) -> tuple[str, list[dict]]:
        """Process one user message and return ``(reply, citations)`` where
        ``citations`` lists the sources the reply is grounded on (each a
        dict with ``source_name`` / ``page`` / ``version_number`` /
        ``category_name``), empty when the turn produced no retrieval."""
        config = {"configurable": {"thread_id": thread_id}}
        if trace_id:
            config["configurable"]["trace_id"] = trace_id

        snapshot = await self.graph.aget_state(config)
        history = list(snapshot.values.get("conversation_history") or [])

        if snapshot.next:
            # A previous turn paused waiting for the user (ticket email
            # collection): this message is the resume value.
            result = await self.graph.ainvoke(
                Command(resume=user_message), config=config
            )
        else:
            result = await self.graph.ainvoke(
                {
                    "user_message": user_message,
                    "conversation_history": history,
                },
                config=config,
            )

        user_turn = ConversationTurn(role="user", content=user_message)
        if "__interrupt__" in result:
            # The turn paused waiting for the user: render the assistant's
            # question as the reply and persist the exchange so the follow-up
            # resume sees the full transcript.
            reply = self._render_interrupt(result["__interrupt__"])
            citations: list[dict] = []
        else:
            # A clarification short-circuits to END with the question in
            # ``clarification_question``; downstream paths land in
            # ``final_response``. Prefer final_response, fall back to the
            # clarification question, then the safe empty fallback.
            reply = result.get("final_response") or result.get(
                "clarification_question"
            ) or ""
            citations = _citations_from_result(result)

        await self.graph.aupdate_state(
            config,
            {
                "conversation_history": [
                    *history,
                    user_turn,
                    ConversationTurn(role="assistant", content=reply),
                ]
            },
        )
        return reply, citations

    @staticmethod
    def _render_interrupt(interrupts: Sequence[Any]) -> str:
        """Turn an active ``interrupt()`` payload into the assistant's
        question text. ``interrupts`` is the ``__interrupt__`` tuple the
        compiled graph publishes; each element exposes ``.value``."""
        if not interrupts:
            return "Please provide more information so I can continue."
        payload = interrupts[0].value
        if isinstance(payload, Mapping):
            question = payload.get("question")
            if question:
                return str(question)
            if payload.get("type") == "email-collection":
                return _EMAIL_COLLECTION_QUESTION
        return str(payload)


async def build_chat_service(
    *,
    llm_client: Optional[SupervisorLLMClient] = None,
    knowledge_graph: Optional[Any] = None,
    checkpointer: Optional[Any] = None,
    embedding_model: Optional[object] = None,
    shared_embeddings: Optional[SharedEmbeddings] = None,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> ChatService:
    """Build the whole chat stack exactly once.

    - ``checkpointer``: leave None to use a MemorySaver default (dev/tests).
    - ``knowledge_graph``: an already-built Knowledge subgraph to inject
      (tests, or when the assembly in this module doesn't fit). When None
      but ``shared_embeddings`` and ``session_factory`` are provided, the
      real Knowledge graph is built here: providers resolved from the
      environment (Agent provider pattern, see ``providers.py``), the
      shared ``embed_query`` injected into ``vector_search``.
    - ``shared_embeddings`` / ``session_factory``: the Phase 6 (§ 4.6)
      construction seam — one BGE instance feeds the shared ``embed_query``
      used by this graph's ``vector_search``.
    - ``ticket_ops`` is always a real ``TicketStore``.
    """
    resolved_client = llm_client or build_llm_client()
    resolved_checkpointer = checkpointer or (await build_checkpointer())

    if knowledge_graph is None and shared_embeddings is not None and session_factory is not None:
        knowledge_graph = _build_real_knowledge_graph(
            shared_embeddings=shared_embeddings,
            session_factory=session_factory,
        )

    graph = build_supervisor_graph(
        llm_client=resolved_client,
        knowledge_graph=knowledge_graph,
        ticket_ops=TicketStore(),
        checkpointer=resolved_checkpointer,
    )
    resolved_embedding = (
        embedding_model
        if embedding_model is not None
        else (shared_embeddings.embedding_model if shared_embeddings is not None else None)
    )
    return ChatService(
        graph=graph,
        llm_client=resolved_client,
        embedding_model=resolved_embedding,
    )


def _build_real_knowledge_graph(
    *,
    shared_embeddings: SharedEmbeddings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Assemble and compile the full Knowledge Agent graph with real
    dependencies: config-driven provider completions + the shared
    ``embed_query`` + the caller's async session factory."""
    config = KnowledgeAgentConfig()
    provider = build_knowledge_provider(config=config)
    completions = llm_completions(provider)
    return build_knowledge_agent_graph(
        config=config,
        rewrite_llm_complete=completions["rewrite_llm_complete"],
        extraction_llm_complete=completions["extraction_llm_complete"],
        answer_llm_complete=completions["answer_llm_complete"],
        session_factory=session_factory,
        embed_query=shared_embeddings.embed_query,
    )
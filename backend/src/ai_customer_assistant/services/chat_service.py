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

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.contracts import ConversationTurn
from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.graph import build_knowledge_agent_graph
from agents.knowledge.providers import build_knowledge_provider, llm_completions
from agents.supervisor.graph import build_supervisor_graph
from agents.supervisor.domain_scope import CorpusScope, static_scope
from agents.supervisor.llm_client import SupervisorLLMClient, build_llm_client
from agents.supervisor.routing import (
    _TURN_TIMEOUT_RESPONSE,
    failure_reason,
    failure_response,
)
from agents.ticket_agent.store import TicketStore, send_ticket_email
from db.checkpointer import build_checkpointer
from services.embeddings import SharedEmbeddings
from agents.supervisor.routing import _BUSY_RESPONSE
from rate_limit_signal import saw_rate_limit, turn_scope
from timeouts import TURN_BUDGET_S

logger = logging.getLogger(__name__)

# How long the stream may stay silent before it says something anyway. Short
# enough that a customer never wonders whether the connection died, long
# enough not to flood a slow turn with noise.
_HEARTBEAT_SECONDS = 5.0

# What the customer is told is happening. Keyed by the Supervisor node that
# just *finished*, because that is what `astream` reports — so each label
# describes the phase now starting, not the one that ended.
_STAGES: Mapping[str, dict] = {
    "start": {"stage": "understanding", "label": "Understanding your question"},
    "classify_and_route": {"stage": "retrieving", "label": "Searching the knowledge base"},
    "ticket_route": {"stage": "ticket", "label": "Setting up your ticket"},
    "knowledge_agent": {"stage": "answering", "label": "Writing your answer"},
    "ticket_agent": {"stage": "answering", "label": "Writing your answer"},
}


def _routes_to_ticket(payload: Any) -> bool:
    """Pure: did classification route this turn to the Ticket Agent?

    Read from the node's own returned update rather than from accumulated
    state: `astream` reports the `updates` chunk *before* the `values` chunk
    that folds it in, so at this moment the state does not know yet. The
    value arrives as a `NextAgent` enum, hence the unwrapping.
    """
    if not isinstance(payload, Mapping):
        return False
    next_agent = payload.get("next_agent")
    return str(getattr(next_agent, "value", next_agent)) == "TICKET_AGENT"


def _stage_after(update: Mapping[str, Any]) -> Optional[dict]:
    """Pure: the stage to announce after `update`'s node finished.

    Classification is the one branch point: it decides whether a ticket or a
    knowledge lookup comes next, and telling someone we are "searching the
    knowledge base" when we are opening them a ticket would be worse than
    saying nothing.
    """
    for node, payload in update.items():
        if node == "classify_and_route" and _routes_to_ticket(payload):
            return _STAGES["ticket_route"]
        stage = _STAGES.get(node)
        if stage is not None:
            return stage
    return None


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
        domain_scope: Optional[CorpusScope] = None,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    ) -> None:
        self.graph = graph
        self.llm_client = llm_client
        # What the classifier treats as in-scope. Refreshed from the chat
        # path rather than read once at startup: the worker ingests
        # continuously, and a definition frozen at boot would refuse
        # questions about a document that had just been added (F6).
        self.domain_scope = domain_scope or static_scope()
        self._session_factory = session_factory
        # The shared BGE instance (Phase 6, §4.6): the same SentenceTransformer
        # the compiled Knowledge graph's `embed_query` was built from, kept here
        # so callers that need the embedding model reuse the same instance —
        # never re-instantiated per request.
        self.embedding_model = embedding_model

    @staticmethod
    async def _has_pending_interrupt(graph: Any, config: dict) -> bool:
        """Detect whether the graph has a pending interrupt that needs resuming.

        ``snapshot.next`` is empty after the *second* interrupt on a
        resumed thread in some LangGraph versions, even though
        ``snapshot.tasks`` still carries the interrupt metadata.  We
        therefore check both signals."""
        snapshot = await graph.aget_state(config)
        if snapshot.next:
            return True
        return any(
            getattr(task, "interrupts", ()) for task in snapshot.tasks
        )

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
        """Process one user message under the turn budget (F1).

        This is the one place that can guarantee the server answers before the
        browser stops listening, because it is the only place that sees a
        whole turn. Bounding the Knowledge node — which is what the ladder used
        to do — bounds only the middle of one:

            turn = classify + knowledge node + checkpoint writes

        Each of those honoured its own budget while the total could reach
        ~76s under a 60s client budget. The customer saw "Error: Request timed
        out" while the server ran on to completion, wrote the checkpoint, and
        threw the answer away — burning provider quota for a reply nobody
        would ever see.

        The budgets below this are sized so cancelling here is a *backstop*.
        A turn that overruns should normally be stopped by the specific layer
        that overran, which can say what went wrong; this can only say "too
        slow". Reaching it regularly means the inner budgets are wrong.

        Cancellation mid-turn leaves the checkpoint wherever the graph had got
        to, so the next message resumes from there. That is the same position
        an aborted request already left it in — except the server now stops
        working too, instead of continuing on a reply that cannot be
        delivered.
        """
        with turn_scope():
            try:
                return await asyncio.wait_for(
                    self._run_turn(thread_id, user_message, trace_id=trace_id),
                    timeout=TURN_BUDGET_S,
                )
            except asyncio.TimeoutError:
                throttled = saw_rate_limit()
                logger.warning(
                    "chat turn exceeded its %.0fs budget and was cancelled "
                    "(thread_id=%s, trace_id=%s)%s",
                    TURN_BUDGET_S,
                    thread_id,
                    trace_id,
                    " (provider was rate limiting)" if throttled else "",
                )
                # Saying "something went wrong" for a turn the provider
                # throttled sends whoever reads it looking for a bug that is
                # not there.
                return (
                    _BUSY_RESPONSE if throttled else _TURN_TIMEOUT_RESPONSE
                ), []

    async def _run_turn(
        self,
        thread_id: str,
        user_message: str,
        *,
        trace_id: Optional[str] = None,
    ) -> tuple[str, list[dict]]:
        """Process one user message and return ``(reply, citations)`` where
        ``citations`` lists the sources the reply is grounded on (each a
        dict with ``source_name`` / ``page`` / ``version_number`` /
        ``category_name``), empty when the turn produced no retrieval.

        The message is always sent to the graph **whole**. An earlier version
        split it on ``?`` and ran the graph once per fragment, which shredded
        any message that merely contained a question mark: ``"Can you help?
        Thanks!"`` became two billed graph runs, a URL with a query string
        was cut in half, and ``"Really?!"`` was split. Compound questions are
        the LLM's job — that is what the prompts are for — not ``str.split``.
        """
        config, history, graph_input = await self._prepare_turn(
            thread_id, user_message, trace_id=trace_id
        )
        result = await self.graph.ainvoke(graph_input, config=config)
        return await self._finalize_turn(result, config, history, user_message)

    async def _prepare_turn(
        self, thread_id: str, user_message: str, *, trace_id: Optional[str] = None
    ) -> tuple[dict, list, Any]:
        """Read the thread's state and decide what to send the graph.

        Split out so the buffered and streaming paths share one definition of
        what a turn *is*. They differ only in how the graph is driven —
        `ainvoke` versus `astream` — and duplicating the resume/history logic
        between them would be two chances to get the ticket flow wrong.
        """
        if self._session_factory is not None:
            await self.domain_scope.refresh_if_stale(self._session_factory)

        config = {"configurable": {"thread_id": thread_id}}
        if trace_id:
            config["configurable"]["trace_id"] = trace_id

        snapshot = await self.graph.aget_state(config)
        history = list(snapshot.values.get("conversation_history") or [])
        pending = snapshot.next or any(
            getattr(task, "interrupts", ()) for task in snapshot.tasks
        )

        # A task that *failed* also stays in the checkpoint, and it keeps its
        # interrupt alongside its error — so "has an interrupt" alone cannot
        # tell a graph waiting for the customer apart from one that crashed
        # mid-flow. Resuming a crashed task replays its stored input, which
        # fails identically, which leaves it crashed: the thread is wedged
        # for good and every later message, on any subject, returns the same
        # error. Observed with one invalid email in the ticket flow, where
        # "hello" three turns later still raised InvalidEmailError on the
        # original text.
        #
        # A healthy pause has an interrupt and no error, so the error is the
        # discriminator. When one is present the pending flow is abandoned
        # and the message starts a fresh turn, which is what the customer
        # meant by sending it. The ticket node no longer raises on a bad
        # address (see agents_wiring), so this is the backstop for the next
        # node that raises, not the fix for that one.
        failed = [task for task in snapshot.tasks if getattr(task, "error", None)]
        if failed and pending:
            logger.warning(
                "abandoning a failed pending task and starting a fresh turn "
                "(thread_id=%s, task=%s, error=%s)",
                thread_id,
                ", ".join(getattr(t, "name", "?") for t in failed),
                str(getattr(failed[0], "error", ""))[:200],
            )
            pending = False

        if pending:
            # A previous turn paused waiting for the user (ticket email
            # collection): this message is the resume value.
            graph_input: Any = Command(resume=user_message)
        else:
            graph_input = {
                "user_message": user_message,
                "conversation_history": history,
            }
        return config, history, graph_input

    async def _finalize_turn(
        self, result: Mapping[str, Any], config: dict, history: list, user_message: str
    ) -> tuple[str, list[dict]]:
        """Turn the graph's final state into ``(reply, citations)``.

        `result` is whatever the graph produced — `ainvoke`'s return, or the
        last `values` chunk of an `astream`, which carry the same keys
        including `__interrupt__` (verified against the compiled graph).
        """
        if "__interrupt__" in result:
            # The turn paused waiting for the user: render the assistant's
            # question as the reply.  Do NOT call aupdate_state here —
            # writing a new checkpoint after an interrupt can clear the
            # pending interrupt in the checkpointer, preventing the next
            # turn from resuming.  The checkpointer already preserves the
            # graph state at the interrupt point; conversation_history will
            # be updated when the interrupted flow completes.
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

            user_turn = ConversationTurn(role="user", content=user_message)
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

    async def stream_message_turn(
        self,
        thread_id: str,
        user_message: str,
        *,
        trace_id: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Run one turn, yielding progress events and finally the reply (F7).

        A turn takes a median of 35 seconds, and until now the customer saw
        an animated ellipsis for all of it — no way to tell a working system
        from a hung one. The events here are coarse by necessity: the
        Knowledge subgraph is invoked *inside* a Supervisor node rather than
        composed as a LangGraph subgraph, so its internal stages are not
        visible to `astream`. Three honest transitions still beat a blank
        spinner, and they arrive at the moments the customer is most likely
        to give up.

        Yielded events, each a plain dict ready to serialise as SSE:

          {"type": "stage",     "stage": ..., "label": ...}
          {"type": "heartbeat", "elapsed": ...}
          {"type": "result",    "reply": ..., "citations": [...]}
          {"type": "error",     "reply": ..., "reason": ...}

        Exactly one terminal event (`result` or `error`) is always yielded,
        so a client can rely on the stream ending with an answer of some
        kind rather than simply stopping.

        Heartbeats matter as much as the stages: they keep the connection
        demonstrably alive across the long silent gap while the answer is
        generated, which is what lets a client wait on *inactivity* instead
        of on total elapsed time.
        """
        started = time.monotonic()
        queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()

        async def produce() -> None:
            try:
                with turn_scope():
                    await asyncio.wait_for(
                        self._drive_turn(thread_id, user_message, trace_id, queue),
                        timeout=TURN_BUDGET_S,
                    )
            except asyncio.TimeoutError:
                throttled = saw_rate_limit()
                logger.warning(
                    "streamed chat turn exceeded its %.0fs budget and was "
                    "cancelled (thread_id=%s, trace_id=%s)%s",
                    TURN_BUDGET_S,
                    thread_id,
                    trace_id,
                    " (provider was rate limiting)" if throttled else "",
                )
                await queue.put(
                    {
                        "type": "error",
                        "reply": _BUSY_RESPONSE if throttled else _TURN_TIMEOUT_RESPONSE,
                        "reason": "rate_limited" if throttled else "timeout",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                logger.warning(
                    "streamed chat turn failed (%s) (thread_id=%s, trace_id=%s)",
                    type(exc).__name__,
                    thread_id,
                    trace_id,
                    exc_info=True,
                )
                await queue.put(
                    {
                        "type": "error",
                        "reply": failure_response(exc),
                        "reason": failure_reason(exc),
                    }
                )
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat", "elapsed": round(time.monotonic() - started, 1)}
                    continue
                if event is None:
                    return
                yield event
        finally:
            # Covers the client hanging up mid-turn: the generator is closed,
            # and the graph must stop too rather than run on for an answer
            # that has nowhere to go — the same waste F1 removed.
            producer.cancel()

    async def _drive_turn(
        self,
        thread_id: str,
        user_message: str,
        trace_id: Optional[str],
        queue: "asyncio.Queue[Optional[dict]]",
    ) -> None:
        """Run the graph with `astream`, pushing stage events as nodes finish."""
        config, history, graph_input = await self._prepare_turn(
            thread_id, user_message, trace_id=trace_id
        )
        await queue.put({"type": "stage", **_STAGES["start"]})

        final_state: dict = {}
        async for mode, chunk in self.graph.astream(
            graph_input, config=config, stream_mode=["updates", "values"]
        ):
            if mode == "values" and isinstance(chunk, dict):
                # The last of these is the whole final state, exactly as
                # `ainvoke` would have returned it.
                final_state = chunk
            elif mode == "updates" and isinstance(chunk, dict):
                stage = _stage_after(chunk)
                if stage is not None:
                    await queue.put({"type": "stage", **stage})

        reply, citations = await self._finalize_turn(
            final_state, config, history, user_message
        )
        await queue.put({"type": "result", "reply": reply, "citations": citations})

    @staticmethod
    def _render_interrupt(interrupts: Sequence[Any]) -> str:
        """Turn an active ``interrupt()`` payload into the assistant's
        question text. ``interrupts`` is the ``__interrupt__`` tuple the
        compiled graph publishes; each element exposes ``.value``."""
        if not interrupts:
            return "Please provide more information so I can continue."
        payload = interrupts[0].value
        if isinstance(payload, Mapping):
            # Explicit type-based handling first
            interrupt_type = payload.get("type")
            if interrupt_type == "email-collection":
                return _EMAIL_COLLECTION_QUESTION
            if interrupt_type == "clarifying_question":
                query = payload.get("query")
                if query:
                    return str(query)
            # Fallback: check for "question" or "query" keys
            for key in ("question", "query"):
                text = payload.get(key)
                if text:
                    return str(text)
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
    scope = CorpusScope() if session_factory is not None else static_scope()

    if knowledge_graph is None and shared_embeddings is not None and session_factory is not None:
        knowledge_graph = _build_real_knowledge_graph(
            shared_embeddings=shared_embeddings,
            session_factory=session_factory,
        )

    graph = build_supervisor_graph(
        llm_client=resolved_client,
        knowledge_graph=knowledge_graph,
        domain_definition=scope.current,
        ticket_ops=TicketStore(
            session_factory=session_factory,
            # The composition root is the only place the real notifier is
            # wired in, so a TicketStore built anywhere else (tests, the
            # graph's own default) never opens an SMTP connection.
            send_email=send_ticket_email if session_factory is not None else None,
        ),
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
        domain_scope=scope,
        session_factory=session_factory,
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
"""Adapter nodes: bridge between the Supervisor's graph and downstream
agents.

Each downstream agent (Knowledge, Ticket) is injected as an already-built
node callable — or, for Knowledge, a compiled subgraph wrapped here —
rather than constructed by the Supervisor. These adapters are
single-responsibility: they only map SupervisorState in, drive the
downstream call, and map the result back out. No routing, no formatting,
no persistence logic lives in this module.

Async note (see agents_integration_plan_new.md §4.5 / open question #5):
the Knowledge Agent's graph is async (`ainvoke`), so once a real
`knowledge_graph` is wired in, the compiled Supervisor graph must be
driven with `ainvoke` — the adapters below are the first async nodes in
the Supervisor graph.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Optional

from langgraph.config import get_config
from langgraph.types import interrupt

from ..contracts import ConversationTurn
from .routing import _SAFE_FALLBACK_RESPONSE
from .schema import SupervisorState

KnowledgeGraph = Callable[[Mapping[str, Any]], Any]


def make_knowledge_agent_node(
    knowledge_graph: KnowledgeGraph,
    timeout_s: float = 120,
) -> Callable[[SupervisorState], Any]:
    """Build the Knowledge Agent adapter node.

    Maps ``SupervisorState`` -> Knowledge state input:
      - ``user_message`` -> ``raw_query``
      - ``conversation_history`` -> flattened role-labeled strings
      - (via ``flatten_history``), matching Knowledge's
      - ``conversation_history: tuple[str, ...]`` channel.

    Only a bounded window of the conversation history is forwarded (the
    last few turns, truncated to a character cap) so the Knowledge
    prompts — which embed the history — stay small and the LLM calls
    finish within ``timeout_s``.

    Runs ``knowledge_graph.ainvoke(...)`` under
    ``asyncio.wait_for(..., timeout_s)``. On timeout or any exception it
    returns a ``downstream_result`` with status ``ERROR`` and the safe
    fallback response.

    Success returns a ``downstream_result`` with status ``GROUNDED`` and
    the answer text as ``response``. Citations are forwarded alongside so
    the serving layer can surface them to the customer.
    """
    async def knowledge_agent(state: SupervisorState) -> dict:
        knowledge_input = {
            "raw_query": state["user_message"],
            "conversation_history": _bounded_history(
                state.get("conversation_history", [])
            ),
        }
        try:
            result = await asyncio.wait_for(
                knowledge_graph.ainvoke(knowledge_input),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return _error_result("timeout")
        except Exception:
            return _error_result("error")

        response = result.get("response")
        if response is None:
            return _error_result("missing_response")

        return {
            "downstream_result": {
                "status": "GROUNDED",
                "response": response.answer_text,
                "citations": list(response.citations),
            }
        }

    return knowledge_agent


# Only the tail of the conversation matters for retrieval context; keeping
# this small bounds the Knowledge prompts and keeps LLM latency in check.
_MAX_HISTORY_TURNS = 4
_MAX_HISTORY_CHARS = 4000


def _bounded_history(history: list[ConversationTurn]) -> tuple[str, ...]:
    """Flatten and trim the conversation history forwarded to Knowledge.

    Keeps the last ``_MAX_HISTORY_TURNS`` turns, then truncates the joined
    text to ``_MAX_HISTORY_CHARS`` characters (whole turns are kept while
    under the cap). Empty when there is nothing to forward.
    """
    turns = tuple(
        f"{turn.role.capitalize()}: {turn.content}"
        for turn in history[-_MAX_HISTORY_TURNS:]
    )
    kept: list[str] = []
    total = 0
    for turn in turns:
        if total + len(turn) > _MAX_HISTORY_CHARS:
            break
        kept.append(turn)
        total += len(turn)
    return tuple(kept)


def _error_result(reason: str) -> dict:
    """DownstreamResult-shaped marker for a failed retrieval.

    The failed turn is surfaced as a safe fallback rather than propagated
    to the customer. ``reason`` is kept for observability."""
    return {
        "downstream_result": {
            "status": "ERROR",
            "response": _SAFE_FALLBACK_RESPONSE,
            "error": reason,
        }
    }


TicketOps = Callable[[], Any]


def _idempotency_key(
    configurable: Mapping[str, Any],
    store: Optional[Any] = None,
) -> str:
    """Derive the idempotency key for a ticket creation.

    Priority, per agents_integration_plan_new.md §2.3:
      1. A client-supplied request ID (``configurable["request_id"]``) — the
         API contract's retry key; a network retry reuses it verbatim.
      2. Server-derived: ``thread_id`` + the store's per-thread ordinal
         (``next_sequence``), so distinct tickets in one thread get distinct
         keys while a retried open of the same ticket maps to the same key.
         Falls back to ``thread_id`` alone when the store isn't a
         sequence-tracking ``TicketStore`` (e.g. a hand-rolled fake).

    The key is NOT a ticket id — it identifies the *request* a Ticket row is
    being created for, which is exactly what ``TicketStore`` de-dupes on.
    """
    request_id = configurable.get("request_id") or configurable.get(
        "idempotency_key"
    )
    if request_id:
        return f"request:{request_id}"

    thread_id = configurable.get("thread_id", "unknown-thread")
    next_sequence = getattr(store, "next_sequence", None)
    if callable(next_sequence):
        return f"{thread_id}:{next_sequence(thread_id)}"
    return thread_id


def make_ticket_agent_node(
    ticket_ops: TicketOps,
) -> Callable[[SupervisorState], dict]:
    """Build the Ticket Agent adapter node.

    Two-step shape, per agents_integration_plan_new.md §2.3 / §4.4:
      1. opening turn: ``ticket_ops.call(query)`` -> ``PendingTicket``, then
          ``interrupt()``s for the email (the checkpointer persists the paused
          state keyed by ``thread_id`` — no ``pending_ticket`` state field is
          needed);
      2. resume turn: ``ticket_ops.create_ticket(pending, email, key)`` -> a
          real ``Ticket``, rendered as a DOWNSTREAM_RESULT-style confirmation
          the assembly node turns into ``final_response``.

    Clarifying question (Phase 5): before asking for the email, the agent
    first asks the user for the reason/purpose of creating the ticket. This
    reason is captured and included in the ticket confirmation.

    Idempotency (Phase 4): the adapter derives an ``idempotency_key`` from the
    runtime config (client ``request_id``, else ``thread_id`` + per-thread
    ordinal) and threads it into ``ticket_ops.create_ticket(...)``. A retried
    resume with the same key hits the store's cache and returns the *existing*
    ticket — no duplicate row.

    The ``CREATE_TICKET`` classification route reaches this node directly.
    ``CHECK_TICKET_STATUS`` is routed away at classification time (§4.4) and
    never reaches this node.
    """
    async def ticket_agent(state: SupervisorState) -> dict:
        query = state.get("user_message", "")
        
        # Phase 5: Ask clarifying question about ticket purpose
        # This will pause the agent and wait for user input
        clarifying_prompt = interrupt(
            {
                "type": "clarifying_question",
                "query": "For what reason do you want to create a ticket?",
            }
        )
        
        # Extract the reason from the clarifying response
        # The interrupt returns a dict with the user's response
        if isinstance(clarifying_prompt, dict):
            clarifying_reason = clarifying_prompt.get("reason", "general inquiry")
        elif isinstance(clarifying_prompt, str):
            clarifying_reason = clarifying_prompt
        else:
            clarifying_reason = "general inquiry"
        
        # Now ask for email with clarifying reason context
        email = interrupt(
            {
                "type": "email-collection",
                "query": query,
                "clarifying_reason": clarifying_reason,
            }
        )
        pending = ticket_ops.call(query)
        configurable = (get_config() or {}).get("configurable", {})
        key = _idempotency_key(configurable, ticket_ops)
        ticket = await ticket_ops.create_ticket(
            pending, email, idempotency_key=key
        )
        confirmation = (
            f"Your ticket has been created (ID {ticket.ticket_id}). "
            f"We'll follow up with you at {ticket.email}."
        )
        return {
            "downstream_result": {
                "status": "GROUNDED",
                "response": confirmation,
            }
        }

    return ticket_agent
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
import inspect
import re
import logging
from typing import Any, Callable, Mapping, Optional

from langgraph.config import get_config
from langgraph.types import interrupt

from timeouts import KNOWLEDGE_NODE_TIMEOUT_S

from ..contracts import ConversationTurn
from .routing import _SAFE_FALLBACK_RESPONSE, failure_reason, failure_response
from .schema import SupervisorState

logger = logging.getLogger(__name__)

KnowledgeGraph = Callable[[Mapping[str, Any]], Any]


def make_knowledge_agent_node(
    knowledge_graph: KnowledgeGraph,
    timeout_s: float = KNOWLEDGE_NODE_TIMEOUT_S,
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
            # Not silent: a node that runs out of budget is the single most
            # useful thing to see in a latency investigation.
            logger.warning(
                "knowledge agent exceeded its %.0fs budget and was cancelled",
                timeout_s,
            )
            return _error_result("timeout")
        except Exception as exc:  # noqa: BLE001 - the turn degrades rather
            # than failing, but the cause has to reach the log. Previously
            # this produced a bare `"error": "error"` and nothing else, which
            # is not enough to distinguish a rate limit from a real defect.
            logger.warning(
                "knowledge agent failed (%s)", type(exc).__name__, exc_info=True
            )
            return _error_result(failure_reason(exc), response=failure_response(exc))

        response = result.get("response")
        if response is None:
            return _error_result("missing_response")

        # `is_grounded` is the model's own verdict on whether it could answer
        # from the material. It was parsed, validated, carried all the way
        # here — and then discarded in favour of a hardcoded "GROUNDED", so a
        # refusal was logged as a success. Reporting it costs nothing and
        # makes an over-refusal visible without reading the answer text.
        return {
            "downstream_result": {
                "status": "GROUNDED" if response.is_grounded else "UNGROUNDED",
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


def _error_result(reason: str, response: Optional[str] = None) -> dict:
    """DownstreamResult-shaped marker for a failed retrieval.

    The failed turn is surfaced as a safe fallback rather than propagated
    to the customer. ``reason`` is kept for observability; ``response``
    overrides the wording so a rate limit can say so instead of claiming
    something is broken."""
    return {
        "downstream_result": {
            "status": "ERROR",
            "response": response or _SAFE_FALLBACK_RESPONSE,
            "error": reason,
        }
    }


TicketOps = Callable[[], Any]


async def _idempotency_key(
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

    A coroutine because the real store now counts the ordinal in the database
    (P2-5): counting it in process memory restarted at zero after a restart
    and reissued a key an earlier ticket in the same thread already held.
    Sync ``next_sequence`` implementations are still accepted so hand-rolled
    test fakes keep working.
    """
    request_id = configurable.get("request_id") or configurable.get(
        "idempotency_key"
    )
    if request_id:
        return f"request:{request_id}"

    thread_id = configurable.get("thread_id", "unknown-thread")
    next_sequence = getattr(store, "next_sequence", None)
    if callable(next_sequence):
        sequence = next_sequence(thread_id)
        if inspect.isawaitable(sequence):
            sequence = await sequence
        return f"{thread_id}:{sequence}"
    return thread_id


def make_ticket_agent_node(
    ticket_ops: TicketOps,
) -> Callable[[SupervisorState], dict]:
    """Build the Ticket Agent adapter node.

    Three-step shape, per agents_integration_plan_new.md §2.3 / §4.4 plus the
    Phase 5 clarifying question:
      1. opening turn: ``interrupt()`` asking *why* the customer wants a
          ticket (the checkpointer persists the paused state keyed by
          ``thread_id`` — no ``pending_ticket`` state field is needed);
      2. reason turn: ``ticket_ops.call(query, reason)`` -> ``PendingTicket``,
          then ``interrupt()`` for the email;
      3. email turn: ``ticket_ops.create_ticket(pending, email, key)`` -> a
          real ``Ticket``, rendered as a DOWNSTREAM_RESULT-style confirmation
          the assembly node turns into ``final_response``.

    The collected reason is threaded into the ``PendingTicket`` — and from
    there into the persisted row, the confirmation email, and the reply the
    customer sees. It is deliberately *not* dropped after being asked for.

    Idempotency (Phase 4): the adapter derives an ``idempotency_key`` from the
    runtime config (client ``request_id``, else ``thread_id`` + per-thread
    ordinal) and threads it into ``ticket_ops.create_ticket(...)``. A retried
    resume with the same key hits the store's cache and returns the *existing*
    ticket — no duplicate row.

    Async because ``ticket_ops.create_ticket`` performs database I/O; the
    compiled Supervisor graph must therefore be driven with ``ainvoke``.

    The ``CREATE_TICKET`` classification route reaches this node directly.
    ``CHECK_TICKET_STATUS`` is routed away at classification time (§4.4) and
    never reaches this node.
    """
    async def ticket_agent(state: SupervisorState) -> dict:
        query = state.get("user_message", "")

        # Step 1 — why does the customer want a ticket? Pauses the graph.
        clarifying_response = interrupt(
            {
                "type": "clarifying_question",
                "query": _TICKET_REASON_QUESTION,
            }
        )
        reason = _resume_text(clarifying_response, key="reason")

        # Step 2 — open the pending ticket with the reason attached, then ask
        # for the email. `call` is pure, so re-running it on each resume (as
        # LangGraph replays the node from the top) is harmless.
        pending = ticket_ops.call(query, reason)
        email = interrupt(
            {
                "type": "email-collection",
                "query": query,
                "clarifying_reason": reason,
            }
        )

        # Step 3 — book the ticket.
        configurable = (get_config() or {}).get("configurable", {})
        key = await _idempotency_key(configurable, ticket_ops)
        ticket = await ticket_ops.create_ticket(
            pending, _resume_text(email, key="email"), idempotency_key=key
        )
        return {
            "downstream_result": {
                "status": "GROUNDED",
                "response": _confirmation(ticket),
            }
        }

    return ticket_agent


def make_ticket_status_node(
    ticket_ops: TicketOps,
) -> Callable[[SupervisorState], dict]:
    """Build the ticket status-lookup node.

    Until now `CHECK_TICKET_STATUS` was answered at classification time with
    a hardcoded "not available yet" -- so a customer who had just been handed
    a ticket id could not ask what had become of it. The intent was already
    classified and the row already existed; only the read path was missing.

    Two shapes, one interrupt at most:

      1. the message already contains an id ("what's the status of
         3f2a...?") -- answer immediately, no round trip;
      2. it does not ("any update on my ticket?") -- `interrupt()` once for
         the id, then answer.

    Identity is the ticket id, not the email address. An email lookup would
    be friendlier and would let anyone who can name an address read that
    person's tickets; the id is the thing the confirmation gave them, and it
    is unguessable.
    """

    async def ticket_status(state: SupervisorState) -> dict:
        message = state.get("user_message", "")

        ticket_id = _find_ticket_id(message)
        if ticket_id is None:
            supplied = interrupt(
                {
                    "type": "ticket_id_collection",
                    "query": _TICKET_ID_QUESTION,
                }
            )
            ticket_id = _find_ticket_id(_resume_text(supplied, key="ticket_id"))

        ticket = await ticket_ops.get_ticket(ticket_id) if ticket_id else None
        return {
            "downstream_result": {
                "status": "GROUNDED",
                "response": _status_reply(ticket_id, ticket),
            }
        }

    return ticket_status


_TICKET_ID_QUESTION = (
    "What is the ticket ID? It's in the confirmation we sent you — "
    "something like 3f2a9c14-5b7e-4a21-9f03-8c6d1e4b7a92."
)

# Deliberately permissive about surroundings and strict about the id itself:
# people paste "ticket 3f2a...?" or "ID: 3f2a...". Anchoring the match to a
# uuid shape means punctuation around it is not the customer's problem.
_TICKET_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _find_ticket_id(text: str) -> str | None:
    """Extract a ticket id, normalized to the canonical lowercase form.

    Customers paste ids in whatever case their mail client rendered them.
    Lowercasing here means the id that gets looked up and the id that gets
    echoed back both match the one in the confirmation.
    """
    match = _TICKET_ID_PATTERN.search(text or "")
    return match.group(0).lower() if match else None


def _status_reply(ticket_id: str | None, ticket: Any) -> str:
    """Render the answer.

    Three outcomes, and they are kept distinct because they need different
    things from the customer: no id given at all, an id that matches nothing,
    and a real ticket.
    """
    if ticket_id is None:
        return (
            "I couldn't find a ticket ID in that. It looks like "
            "3f2a9c14-5b7e-4a21-9f03-8c6d1e4b7a92 and is in the confirmation "
            "we sent when the ticket was opened."
        )

    if ticket is None:
        # Not "that ticket is closed" and not an error: an id that matches
        # nothing is usually a typo, and saying so is more useful than
        # implying the ticket once existed.
        return (
            f"I couldn't find a ticket with ID {ticket_id}. Please double-check "
            f"it against the confirmation we sent — or tell me what you need and "
            f"I can open a new ticket."
        )

    status = (getattr(ticket, "status", None) or "OPEN").replace("_", " ").lower()
    reason = getattr(ticket, "reason", None)
    about = f' about "{reason}"' if reason else ""
    return (
        f"Ticket {ticket.ticket_id}{about} is currently **{status}**. "
        f"We'll follow up with you at {ticket.email}."
    )


_TICKET_REASON_QUESTION = "For what reason do you want to create a ticket?"


def _resume_text(resume_value: object, *, key: str) -> str:
    """Normalize whatever ``interrupt()`` handed back into plain text.

    The serving layer resumes with the customer's raw message (a string); the
    mapping branch exists for callers that resume with a structured payload.
    """
    if isinstance(resume_value, Mapping):
        return str(resume_value.get(key) or "").strip()
    return str(resume_value or "").strip()


def _confirmation(ticket: Any) -> str:
    """Render the customer-facing confirmation, echoing back the reason they
    gave so the answer to the clarifying question is visibly used."""
    reason = getattr(ticket, "reason", None)
    reason_clause = f' regarding "{reason}"' if reason else ""
    return (
        f"Your ticket has been created (ID {ticket.ticket_id}){reason_clause}. "
        f"We'll follow up with you at {ticket.email}."
    )
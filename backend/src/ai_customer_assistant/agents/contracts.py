"""
contracts.py — canonical cross-agent contracts.

The single source of truth for every shape that crosses an agent
boundary in this system. No package constructs these as bare dicts
or re-declares them locally (see agents_integration_plan_new.md §3):

- ``DownstreamResult``: the hand-off every downstream agent (or the
  Safety gate) produces for the Supervisor Agent.
- ``ConversationTurn`` / ``flatten_history``: the one representation of
  conversation history every agent consumes.

These types are framework-neutral (Pydantic, no LangGraph import) so
the Supervisor, Knowledge, Ticket, and Safety packages can all be
implemented and unit-tested against them independently, before any
graph wiring exists.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class DownstreamStatus(str, Enum):
    """Verdict of one downstream agent turn, as seen by the Supervisor.

    - ``GROUNDED``: final answer ready to show the customer.
    - ``ERROR``: the agent failed (timeout / infra), never propagates
      to the API layer.
    """

    GROUNDED = "GROUNDED"
    ERROR = "ERROR"


class DownstreamResult(BaseModel):
    """Typed replacement for the bare
    ``{"status", "response"}`` dict v1
    used.

    Observability fields (``agent_name``, ``latency_ms``, ``citations``,
    ``confidence``) are logged with a ``trace_id`` and never rendered to
    the customer. ``schema_version`` lets a future field addition fail
    loudly for consumers pinned to an older shape instead of silently
    misreading it.
    """

    status: DownstreamStatus
    response: str
    schema_version: int = 1

    agent_name: str | None = None
    latency_ms: float | None = None
    citations: tuple[str, ...] = ()
    confidence: float | None = None


class ConversationTurn(BaseModel):
    """One message in a conversation. ``role`` is strictly the message
    author; agent-internal naming (e.g. Gemini's "model") is mapped at
    the provider boundary."""

    role: Literal["user", "assistant"]
    content: str


def flatten_history(history: list[ConversationTurn]) -> tuple[str, ...]:
    """Render a conversation for agents that need a flat sequence,
    preserving who said what (e.g. Knowledge Agent rewrite/extract
    prompts).

    ``"User: <content>"`` / ``"Assistant: <content>"`` — decoupled from
    any specific agent's internal formatting so the Knowledge Agent's
    numbered-turn rendering stays the only place that numbers turns.
    """
    return tuple(f"{turn.role.capitalize()}: {turn.content}" for turn in history)
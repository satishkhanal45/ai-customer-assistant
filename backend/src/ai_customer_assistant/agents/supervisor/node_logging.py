"""Node tracing that does not put the customer's data on stdout (P0-4).

## What this replaces

Every Supervisor node entry and exit did:

    print(f"--- supervisor node: {name} (in) ---")
    print(json.dumps(_jsonable(state), indent=2))

unconditionally, in the Docker image, with no log level and no guard. What
that printed on a ticket turn was the customer's raw message, the entire
conversation history, and — in the confirmation text — **their email
address**. Anyone with access to the container logs had the transcript.

Three separate problems, and the privacy one is only the first:

1. It cannot be turned off. There is no level to raise and no flag to unset.
2. `print()` bypasses logging entirely, so it ignores handlers, formatting
   and `LOG_LEVEL`, and under `PYTHONUNBUFFERED=1` it is a synchronous write
   per node — real latency on every turn, paid whether anyone is reading.
3. It serialises the whole state to indented JSON *before* deciding anything,
   so the cost is incurred even when the output is discarded.

## What replaces it

`log_node` wraps a node the same way, but:

- emits at DEBUG through the logging module, so it is off under the default
  `INFO` level and obeys `LOG_LEVEL` like everything else;
- checks `isEnabledFor(DEBUG)` **before** serialising, so a disabled trace
  costs one integer comparison rather than a JSON dump;
- redacts free-text fields even when tracing *is* on.

That last point is the one worth arguing for. The obvious fix is a flag that
turns the whole dump on and off, and it fails the moment someone needs to
debug a live routing problem: the only way to see which branch a turn took
is to also print the customer's message. So the default keeps every routing
field — category, intent, confidences, next agent, ticket type, error — and
replaces free text with a shape summary:

    "user_message": "<redacted str, 34 chars>"
    "conversation_history": "<redacted list, 6 items>"

which is enough to debug routing, and enough to see that history is growing,
without reproducing what anyone said. Setting `LOG_PII=true` opts back in,
deliberately and visibly, for a local session.
"""

from __future__ import annotations

import inspect
import json
import logging
import os

from .schema import SupervisorState

logger = logging.getLogger(__name__)

LOG_PII_ENV_VAR = "LOG_PII"

# Fields that carry what a customer wrote, what we told them, or who they
# are. `response` / `final_response` are here because the ticket confirmation
# embeds the email address in its body — redacting `email` alone would miss
# it, which is exactly the kind of near-miss that makes allowlists safer than
# denylists for anything that actually matters.
_SENSITIVE_KEYS = frozenset(
    {
        "answer_text",
        "citations",
        "clarification_question",
        "conversation_history",
        "email",
        "final_response",
        "query",
        "raw_query",
        "reason",
        "response",
        "user_message",
    }
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def pii_logging_enabled() -> bool:
    """Whether the operator has explicitly opted into logging customer text."""
    return os.environ.get(LOG_PII_ENV_VAR, "").strip().lower() in _TRUTHY


def jsonable(value: object) -> object:
    """Best-effort JSON conversion so any state value (dataclasses, enums,
    provenance objects) can be rendered for a debug trace."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def _summarise(value: object) -> str:
    """A redaction that still says something useful.

    Keeping the type and size means a trace can show that history is growing,
    or that an answer came back empty, without carrying the content.
    """
    if isinstance(value, str):
        return f"<redacted str, {len(value)} chars>"
    if isinstance(value, (list, tuple)):
        return f"<redacted list, {len(value)} items>"
    if isinstance(value, dict):
        return f"<redacted dict, {len(value)} keys>"
    if value is None:
        return "<none>"
    return f"<redacted {type(value).__name__}>"


def redact(value: object, *, include_pii: bool = False) -> object:
    """Pure: replace free-text fields with a shape summary, recursively.

    Applied to the already-JSON-able structure so it sees plain dicts and
    lists whatever the state held originally.
    """
    if include_pii:
        return value
    if isinstance(value, dict):
        return {
            key: _summarise(item) if key in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _render(label: str, payload: object) -> None:
    logger.debug(
        "supervisor node: %s\n%s",
        label,
        json.dumps(redact(jsonable(payload), include_pii=pii_logging_enabled()), indent=2),
    )


def log_node(node_name: str, node_fn):
    """Wrap a node so its state and update are traceable at DEBUG.

    Works for both sync and async node functions. ``interrupt()`` pauses
    propagate untouched — they raise through the wrapper, which does nothing
    to catch them.
    """
    if inspect.iscoroutinefunction(node_fn):

        async def _async_logged(state: SupervisorState) -> dict:
            enabled = logger.isEnabledFor(logging.DEBUG)
            if enabled:
                _render(f"{node_name} (in)", state)
            result = await node_fn(state)
            if enabled:
                _render(f"{node_name} (out)", result)
            return result

        return _async_logged

    def _sync_logged(state: SupervisorState) -> dict:
        enabled = logger.isEnabledFor(logging.DEBUG)
        if enabled:
            _render(f"{node_name} (in)", state)
        result = node_fn(state)
        if enabled:
            _render(f"{node_name} (out)", result)
        return result

    return _sync_logged

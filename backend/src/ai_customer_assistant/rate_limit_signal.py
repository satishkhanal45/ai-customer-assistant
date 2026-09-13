"""Whether the provider throttled us during this turn.

A rate limit and a slow answer look identical once the Knowledge node's
wall-clock budget expires: both surface as `asyncio.TimeoutError`, and the
customer is told "something went wrong on my end" — which sends whoever reads
it looking for a bug that is not there. It happened in this project: a free
Groq tier allows 8,000 tokens a minute and one chat turn measured **6,871**,
so the second question inside a minute was always throttled, always timed out,
and always reported as an unknown failure.

The provider already raises a usable error, and `routing.failure_response`
already turns that into the right message. The gap is only that a *timeout*
carries no cause — so the provider leaves a note here, and the node reads it.

**Why a mutable object rather than a plain ContextVar value.** The provider
call runs under `asyncio.to_thread`, which executes it in a *copy* of the
context. `ContextVar.set()` inside that copy is invisible to the caller, so a
boolean would never make it back. The copy shares the same dict *object*,
though, so mutating it is visible on both sides.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_TURN: ContextVar[dict | None] = ContextVar("turn_rate_limit", default=None)


@contextmanager
def turn_scope() -> Iterator[dict]:
    """Observe one turn's throttling.

    Re-entrant on purpose. Two layers time a turn -- the whole turn in
    `ChatService`, the Knowledge node inside it -- and both need the answer.
    A nested scope that started its own observation would hide the inner
    layer's from the outer one, so the outer timeout would report a plain
    timeout for a turn the provider had visibly throttled.
    """
    existing = _TURN.get()
    if existing is not None:
        yield existing
        return

    state: dict = {"rate_limited": False}
    token = _TURN.set(state)
    try:
        yield state
    finally:
        _TURN.reset(token)


def note_rate_limited() -> None:
    """Record that the provider throttled a call in this turn.

    A no-op outside a turn scope, so a provider used directly — by a test, a
    script, the ingestion worker — needs no setup.
    """
    state = _TURN.get()
    if state is not None:
        state["rate_limited"] = True


def saw_rate_limit() -> bool:
    state = _TURN.get()
    return bool(state and state["rate_limited"])

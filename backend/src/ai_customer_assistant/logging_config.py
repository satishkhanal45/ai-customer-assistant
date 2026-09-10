"""Application log configuration.

Deliberately *not* in `main.py`. That module is the ASGI entry point and
calls `config.load_env()` at import, which is correct for an entry point and
poisonous for a test process: importing it injects every value in
`backend/.env` into `os.environ`, and the `skipif` guards on the tests that
need a live Postgres then believe one is configured. A unit test should be
able to check the log setup without dragging the environment in with it —
which is the same lesson as P0-2, one level up.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


# Loggers that are useful at WARNING and merely noisy at INFO — the HTTP
# stacks under the model providers narrate every request and retry.
NOISY_LIBRARIES = ("httpx", "httpcore", "urllib3", "openai", "groq", "anthropic")


def configure_logging() -> None:
    """Make the application's own INFO logging actually appear.

    Uvicorn configures handlers for *its* loggers and leaves the root at
    WARNING, so `logger.info(...)` anywhere in this codebase was silently
    discarded. That was not obvious: the F3 warnings came through (they clear
    WARNING) while the retrieval-strategy line added for F5 did not, so the
    decision that explains why two runs of one question retrieved differently
    was still invisible in a deployed process — which was the whole point of
    logging it.

    `LOG_LEVEL` overrides the default. Third-party HTTP stacks are pinned to
    WARNING regardless, or every model call narrates itself.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)
    # Before anything logs: the format names `trace_id`, and a record without
    # that attribute raises inside logging rather than printing.
    install_trace_filter()
    for noisy in NOISY_LIBRARIES:
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Correlating log lines with the work that produced them
# ---------------------------------------------------------------------------
#
# The chat path threads a `trace_id` through its LangGraph config, but it only
# ever reached a handful of error messages, and ingestion had none at all --
# so a document's log lines could not be told from any other document's. That
# became a real problem twice over: a job may now be *retried*, so one
# `job_id` can produce four separate runs, and the worker may run several
# documents at once, so those runs interleave.
#
# A ContextVar rather than a parameter, because the lines that need labelling
# are emitted deep inside modules that have no reason to know about jobs --
# `ingestion.extraction.agent` warns about a dropped window, `persistence`
# about a missing chunk. Threading an id through every one of them would be
# a worse trade than reading it from the ambient context.
#
# ContextVars are copied per asyncio task and, importantly, propagated by
# `asyncio.to_thread`, so a line logged from the extraction thread carries the
# same id as the coroutine that started it.

_TRACE_ID: ContextVar[str | None] = ContextVar("trace_id", default=None)

# What an unlabelled line shows. A literal placeholder rather than an empty
# string keeps the columns aligned, which is the whole reason to put it in the
# format string rather than the message.
NO_TRACE = "-"


class TraceIdFilter(logging.Filter):
    """Stamp the ambient trace id onto every record passing through a handler.

    Attached to the *handler* rather than a logger: a filter on a logger is
    not applied to records that reach the root handler by propagation, and
    propagation is how almost every line in this codebase gets emitted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _TRACE_ID.get() or NO_TRACE
        return True


def install_trace_filter() -> None:
    """Make `%(trace_id)s` safe to use in a format string.

    Must be called after the handler exists and before anything logs through
    it: a format that names `trace_id` on a record that lacks the attribute
    raises inside logging itself. Idempotent, so an entry point that calls it
    twice does not stack filters.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(TraceIdFilter())


@contextmanager
def trace_context(trace_id: str) -> Iterator[str]:
    """Label every log line emitted inside this block with `trace_id`."""
    token = _TRACE_ID.set(trace_id)
    try:
        yield trace_id
    finally:
        _TRACE_ID.reset(token)


def current_trace_id() -> str | None:
    return _TRACE_ID.get()

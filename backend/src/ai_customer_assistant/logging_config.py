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
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)
    for noisy in NOISY_LIBRARIES:
        logging.getLogger(noisy).setLevel(logging.WARNING)

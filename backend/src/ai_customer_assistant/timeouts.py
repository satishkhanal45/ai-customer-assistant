"""The request timeout ladder, in one place (P2-4).

## The defect this exists to prevent

The browser gave up on `POST /chat` after 60 seconds. The Knowledge node was
allowed 120, and a single rate-limited Groq call could sleep for up to 300
before its first retry. So a turn that took 60-120 seconds was **aborted in
the browser while the server kept going** — it finished the answer, wrote the
checkpoint, and threw the reply away. The customer saw "Request timed out",
and their next message resumed from a conversation state they had never been
shown.

Nothing was logged, because from each layer's own point of view nothing went
wrong: the client timed out on schedule and the server completed successfully.

## The rule

Every budget must be strictly smaller than the budget of the layer that is
waiting on it, so **the innermost layer always gives up first** and the
failure surfaces as a real error message rather than an abandoned request:

    client (60s)  >  knowledge node (45s)  >  one LLM completion (25s)
                                              >  one HTTP call (15s)

`assert_ladder_is_consistent()` runs at import and refuses to start on a
violation. That is deliberate: an inverted ladder produces no error anywhere,
so a loud failure at boot is the only moment it is cheap to notice.

## Overriding

Each value may be overridden by environment variable for a deployment whose
latency profile differs (a slower model, a proxy with its own cutoff). Keep
the ordering — the check will tell you if you didn't. The client budget lives
in the browser, so it is declared here only as the reference point the server
budgets must stay under; `frontend/src/pages/chat.js` carries the matching
literal and `tests/test_timeout_ladder.py` asserts the two agree.
"""

from __future__ import annotations

import os


def _float_env(name: str, default: float) -> float:
    """Read a float from the environment, falling back on anything
    unparseable rather than killing the process over a typo'd value —
    the same policy `db/engine.py` uses for pool sizing."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# What the browser waits for a `POST /chat` response before aborting. Must
# match `CHAT_REQUEST_TIMEOUT_MS` in frontend/src/pages/chat.js.
CLIENT_REQUEST_TIMEOUT_S: float = _float_env("CLIENT_REQUEST_TIMEOUT_S", 60.0)

# How long the Supervisor lets the whole Knowledge subgraph run — rewrite,
# extraction, retrieval, ranking and answer generation together — before
# cancelling it and returning a graceful failure. Strictly under the client
# budget so the user sees that message instead of a browser-side abort.
KNOWLEDGE_NODE_TIMEOUT_S: float = _float_env("KNOWLEDGE_NODE_TIMEOUT_S", 45.0)

# Wall-clock ceiling for one logical LLM completion including every retry and
# backoff sleep. The Knowledge subgraph makes three completions per turn, so
# this is not a per-turn budget — the node timeout above is the real ceiling.
# What this bounds is a *single* call's ability to sit in a backoff sleep for
# longer than the caller is prepared to wait, which is what Groq's unbounded
# "please try again in 6m33s" hint used to cause.
LLM_RETRY_BUDGET_S: float = _float_env("LLM_RETRY_BUDGET_S", 25.0)

# Socket timeout for one HTTP request to a model provider.
LLM_CALL_TIMEOUT_S: float = _float_env("LLM_CALL_TIMEOUT_S", 15.0)


def assert_ladder_is_consistent() -> None:
    """Raise if any layer is allowed to outlive the layer waiting on it."""
    ladder = (
        ("LLM_CALL_TIMEOUT_S", LLM_CALL_TIMEOUT_S),
        ("LLM_RETRY_BUDGET_S", LLM_RETRY_BUDGET_S),
        ("KNOWLEDGE_NODE_TIMEOUT_S", KNOWLEDGE_NODE_TIMEOUT_S),
        ("CLIENT_REQUEST_TIMEOUT_S", CLIENT_REQUEST_TIMEOUT_S),
    )
    for (inner_name, inner), (outer_name, outer) in zip(ladder, ladder[1:]):
        if inner > outer:
            raise ValueError(
                f"timeout ladder inverted: {inner_name}={inner} exceeds "
                f"{outer_name}={outer}. The inner layer must always give up "
                f"first, or the outer one abandons a request that is still "
                f"running. See timeouts.py."
            )


assert_ladder_is_consistent()

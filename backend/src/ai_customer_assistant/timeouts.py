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

The server must always answer before the client stops listening, and every
inner budget must be small enough that the layer *outside* it never has to
give up first. Two things are required for that, and only having the first
is what made this ladder wrong for a while:

1. Each rung is smaller than the rung around it.
2. The rungs that run **in sequence** sum to less than the budget containing
   them. Every rung being individually small enough is not sufficient.

    client 60s
      > turn 52s                         (hard ceiling, enforced in ChatService)
          = classify 12s
          + knowledge node 37s           (derived: whatever is left)
          + checkpointing 3s

Within the Knowledge node the two innermost rungs are **per stage**, not
global — see "Per-stage LLM budgets" below for the measurements that forced
that split:

    short  (classify / rewrite / extract):  call 10s,  with retries 22s
    answer (generation):                    call 30s,  with retries 34s

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
import time


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

# --------------------------------------------------------------------------
# The turn budget (F1)
#
# The server's own hard ceiling on one `POST /chat`, enforced in
# `ChatService.handle_message_turn`. This is the guarantee the whole ladder
# exists to provide, and until it was added the ladder did not actually
# provide it: bounding the Knowledge node bounded only the middle of a turn.
#
#     turn = classify  +  knowledge node  +  checkpoint writes
#
# With the node at 45s and classification able to burn 3 x 10s of socket
# timeout plus backoff, a turn could reach ~76s while every individual rung
# honoured its budget — and the browser stopped listening at 60. The customer
# saw "Error: Request timed out" while the server carried on to completion,
# wrote the checkpoint, and discarded the answer.
#
# Everything below is sized so this ceiling is a *backstop* rather than the
# normal way a slow turn ends. A turn that overruns should be stopped by the
# specific layer that overran — which produces a specific error — not by the
# guillotine, which can only say "too slow".
# --------------------------------------------------------------------------

TURN_BUDGET_S: float = _float_env("TURN_BUDGET_S", 52.0)

# Wall-clock ceiling for classification, including its retries. Measured at
# 1.1s; the allowance covers one full socket timeout plus a fast retry.
CLASSIFY_BUDGET_S: float = _float_env("CLASSIFY_BUDGET_S", 12.0)

# Left for the work that is neither classification nor retrieval: reading the
# checkpoint at the start of the turn, writing it at the end, and serialising
# the response.
CHECKPOINT_HEADROOM_S: float = _float_env("CHECKPOINT_HEADROOM_S", 3.0)

# How long the Supervisor lets the whole Knowledge subgraph run — rewrite,
# extraction, retrieval, ranking and answer generation together — before
# cancelling it and returning a graceful failure.
#
# Derived, not declared: it is whatever the turn budget has left once
# classification and checkpointing are paid for. Declaring it independently
# is what let the arithmetic drift out of agreement with the client in the
# first place. An explicit env override still wins, and the consistency check
# below will reject it if it does not fit.
KNOWLEDGE_NODE_TIMEOUT_S: float = _float_env(
    "KNOWLEDGE_NODE_TIMEOUT_S",
    TURN_BUDGET_S - CLASSIFY_BUDGET_S - CHECKPOINT_HEADROOM_S,
)

# --------------------------------------------------------------------------
# Per-stage LLM budgets
#
# One number for every LLM call was wrong, and live testing showed why. The
# four calls in a turn have completely different cost profiles, measured
# against the real corpus on Groq's `openai/gpt-oss-120b`:
#
#     classify   1.1s
#     rewrite    0.5 - 1.9s
#     extract    1.0 - 1.4s
#     answer     1.3 - 13.6s        <-- an order of magnitude wider
#
# Classification and rewriting emit a few tokens of JSON. Answer generation
# emits paragraphs, and generation time scales with output length, so its
# spread is intrinsic rather than a symptom of anything being wrong.
#
# The shared 15s timeout sat right on top of the answer stage's real range.
# A healthy 16-second generation was cut off as a failure; the retry then
# cost another 15s, exhausting the budget, and the Knowledge node returned a
# bare `"error": "error"` for a call that would have succeeded. Meanwhile
# classification, which needs about a second, was allowed fifteen — so a
# genuinely stuck classify call held the turn open far longer than necessary
# before anyone gave up on it.
# --------------------------------------------------------------------------

# classify / rewrite / extract — short, structured completions.
LLM_SHORT_TIMEOUT_S: float = _float_env("LLM_SHORT_TIMEOUT_S", 10.0)
LLM_SHORT_RETRY_BUDGET_S: float = _float_env("LLM_SHORT_RETRY_BUDGET_S", 22.0)

# Answer generation — long, and legitimately so.
LLM_ANSWER_TIMEOUT_S: float = _float_env("LLM_ANSWER_TIMEOUT_S", 30.0)
LLM_ANSWER_RETRY_BUDGET_S: float = _float_env("LLM_ANSWER_RETRY_BUDGET_S", 34.0)

# Kept as the default for provider constructors and for any call site that
# has no stage-specific opinion. It is the short budget: a caller that has
# not said it is generating prose does not get the generous allowance.
LLM_CALL_TIMEOUT_S: float = LLM_SHORT_TIMEOUT_S
LLM_RETRY_BUDGET_S: float = LLM_SHORT_RETRY_BUDGET_S

# --------------------------------------------------------------------------
# Ingestion budgets
#
# Deliberately NOT part of the ladder above. That ladder exists because a
# person is waiting on a chat turn and the client gives up at 60 seconds.
# Ingestion is background work with nobody watching, so it can afford to be
# slower — but "slower" is not "unbounded", which is what it was.
#
# Observed 2026-09-09: one document sat in RUNNING for 28 minutes with no
# log output and no progress. The worker finishes one job before claiming
# another, so the entire queue stopped behind it, and the stale-job reaper
# could not help — it runs once at worker startup, so a worker that is alive
# and stuck blocks forever. A single hung HTTP call became an indefinitely
# stalled pipeline.
#
# Two numbers, because they bound different things:
#
#   * the CALL timeout is handed to the provider client, so the underlying
#     HTTP request actually terminates. This is the one that unwedges a
#     stuck thread;
#   * the STAGE budget bounds the whole document, because extraction makes
#     one call per chunk and a 40-chunk document can be slow without any
#     single call being slow.
#
# 30s per call is three times the chat short-stage timeout: extraction emits
# structured JSON over a chunk of prose, and nobody is watching a progress
# spinner. 10 minutes for a document is generous enough that hitting it
# means something is genuinely wrong rather than merely large.
INGEST_EXTRACTION_CALL_TIMEOUT_S: float = _float_env(
    "INGEST_EXTRACTION_CALL_TIMEOUT_S", 30.0
)
INGEST_EXTRACTION_STAGE_BUDGET_S: float = _float_env(
    "INGEST_EXTRACTION_STAGE_BUDGET_S", 600.0
)

# How many times the provider client retries a transient failure of its own
# accord. The SDK's backoff is what absorbed every 429 in the 2026-09-09
# re-run, so it is worth keeping — but bounded, so the retries fit inside
# the call timeout rather than extending it indefinitely.
INGEST_EXTRACTION_MAX_RETRIES: int = int(
    _float_env("INGEST_EXTRACTION_MAX_RETRIES", 3)
)


def sleep_within_budget(requested: float, deadline: float, call_timeout: float) -> bool:
    """Sleep for ``requested`` seconds, but only if a retry still fits.

    Returns False (and sleeps not at all) when the remaining budget cannot
    hold both this backoff and the attempt it precedes — there is no point
    waiting for a call the caller will have abandoned before it returns.
    Otherwise sleeps for at most the remaining budget and returns True.

    Shared by the Knowledge provider and the Supervisor's classifier so both
    retry loops are bounded by wall clock rather than by attempt count. A
    loop bounded only by attempts can always be made to outlive its caller
    by a provider that asks it to wait long enough.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or remaining < requested + call_timeout:
        return False
    time.sleep(min(requested, remaining))
    return True


def assert_ladder_is_consistent() -> None:
    """Raise if any layer is allowed to outlive the layer waiting on it.

    Three rules, because a single descending chain does not describe this
    system once the budgets are per stage:

    1. Within a stage, one call must not outlive the retry loop wrapping it.
    2. No stage's *whole* retry budget may reach the Knowledge node's
       ceiling — otherwise one slow completion consumes the entire budget
       the node needed for three of them plus retrieval.
    3. The server-side budgets must **sum** to no more than the turn budget,
       and the turn budget must sit under the client's. Checking each pair
       in isolation is what missed F1: every rung was individually smaller
       than the client budget while the total was larger.
    """
    for timeout_name, timeout, budget_name, budget in (
        ("LLM_SHORT_TIMEOUT_S", LLM_SHORT_TIMEOUT_S,
         "LLM_SHORT_RETRY_BUDGET_S", LLM_SHORT_RETRY_BUDGET_S),
        ("LLM_ANSWER_TIMEOUT_S", LLM_ANSWER_TIMEOUT_S,
         "LLM_ANSWER_RETRY_BUDGET_S", LLM_ANSWER_RETRY_BUDGET_S),
        ("LLM_SHORT_TIMEOUT_S", LLM_SHORT_TIMEOUT_S,
         "CLASSIFY_BUDGET_S", CLASSIFY_BUDGET_S),
    ):
        if timeout > budget:
            raise ValueError(
                f"timeout ladder inverted: {timeout_name}={timeout} exceeds "
                f"{budget_name}={budget}. A single call must not outlive the "
                f"retry loop wrapping it. See timeouts.py."
            )

    for budget_name, budget in (
        ("LLM_SHORT_RETRY_BUDGET_S", LLM_SHORT_RETRY_BUDGET_S),
        ("LLM_ANSWER_RETRY_BUDGET_S", LLM_ANSWER_RETRY_BUDGET_S),
    ):
        if budget >= KNOWLEDGE_NODE_TIMEOUT_S:
            raise ValueError(
                f"timeout ladder inverted: {budget_name}={budget} reaches "
                f"KNOWLEDGE_NODE_TIMEOUT_S={KNOWLEDGE_NODE_TIMEOUT_S}. One "
                f"completion would consume the whole node budget, which has "
                f"to cover three of them plus retrieval. See timeouts.py."
            )

    server_side = CLASSIFY_BUDGET_S + KNOWLEDGE_NODE_TIMEOUT_S + CHECKPOINT_HEADROOM_S
    if server_side > TURN_BUDGET_S:
        raise ValueError(
            f"timeout ladder inverted: the server-side budgets sum to "
            f"{server_side}s (classify {CLASSIFY_BUDGET_S} + knowledge node "
            f"{KNOWLEDGE_NODE_TIMEOUT_S} + checkpointing "
            f"{CHECKPOINT_HEADROOM_S}) but TURN_BUDGET_S={TURN_BUDGET_S}. "
            f"Every rung being individually small enough is not sufficient — "
            f"this exact gap is what let a turn run to ~76s under a 60s "
            f"client budget. See timeouts.py."
        )

    if TURN_BUDGET_S >= CLIENT_REQUEST_TIMEOUT_S:
        raise ValueError(
            f"timeout ladder inverted: TURN_BUDGET_S={TURN_BUDGET_S} reaches "
            f"CLIENT_REQUEST_TIMEOUT_S={CLIENT_REQUEST_TIMEOUT_S}. The server "
            f"must answer before the browser stops listening. See timeouts.py."
        )


assert_ladder_is_consistent()

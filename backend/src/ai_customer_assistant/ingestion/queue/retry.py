"""When a failed job should be tried again, and when it should be given up on.

Every failure used to be terminal. `complete_job` wrote `FAILED` and that was
the end of the document -- so a job that hit a per-minute rate limit, or a
Tika that happened to be restarting, needed a human to notice and re-queue it
by hand. In this deployment that is what most of the failure count actually
was: after the deterministic failures were fixed (`ingestion.md` items 8, 8b)
the two remaining documents failed on a *daily* token ceiling, which a retry
an hour later simply walks past.

The policy is deliberately a pure function of (failure kind, attempts so far).
It makes no I/O and reads no clock beyond `now`, so the interesting question
-- "does this failure deserve another go?" -- is answerable in a unit test
without a database or a provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Failure kinds worth trying again. Everything not named here is terminal by
# default, which is the safe direction: a new failure kind that nobody has
# classified yet stops after one attempt and stays visible, rather than
# quietly consuming the whole retry budget on every job that hits it.
#
# Each entry earns its place:
#   tika_transient              -- already classified as transient by the
#                                  extraction stage itself.
#   eav_extraction_rate_limited -- provider throttling. The single most common
#                                  failure here, and the one that motivated
#                                  this module: quota returns on its own.
#   eav_extraction_timeout      -- the document ran past its wall-clock budget.
#                                  Often a symptom of throttling upstream
#                                  (calls crawling while the bucket refills),
#                                  so it is worth one more run on a quiet
#                                  queue -- bounded by the attempt cap.
#   storage_fetch_failed        -- MinIO unreachable or a connection dropped.
#   worker_abandoned            -- the worker died mid-job. Retryable so a
#                                  deploy does not strand a document, but
#                                  counted, so a job that kills the worker
#                                  every time cannot loop forever.
TRANSIENT_KINDS: frozenset[str] = frozenset(
    {
        "tika_transient",
        "eav_extraction_rate_limited",
        "eav_extraction_timeout",
        "storage_fetch_failed",
        "worker_abandoned",
    }
)

# The kind recorded when a worker dies mid-job and the reaper requeues the row.
WORKER_ABANDONED = "worker_abandoned"

# Transient kinds that are retried *immediately* rather than after a backoff.
#
# A backoff answers "the condition that caused this needs time to clear" --
# a token bucket refilling, a Tika coming back up. A worker that died has
# already cleared by definition: the process reaping the row is its
# replacement. Making an ordinary deploy cost every in-flight document ten
# idle minutes buys nothing, and the attempt cap still applies, which is what
# actually protects against a document that kills every worker that touches
# it.
IMMEDIATE_KINDS: frozenset[str] = frozenset({WORKER_ABANDONED})

# Four attempts, not more. Every retry re-runs the *whole* pipeline -- fetch,
# Tika, chunk, embed, extract -- so an attempt is expensive, and against a
# daily token quota a failing job that retries forever is actively harmful:
# it spends the budget that the jobs which would succeed need.
MAX_ATTEMPTS: int = max(1, int(os.environ.get("INGEST_JOB_MAX_ATTEMPTS", "4")))

# Backoff base and ceiling. Ten minutes, then 20, then 40 -- long enough for a
# per-minute token bucket to refill many times over, and for a Tika restart or
# a deploy to finish. The ceiling keeps the last attempt inside the same
# working day, so a document that fails in the morning is not first retried
# after midnight.
BACKOFF_BASE_SECONDS: float = float(
    os.environ.get("INGEST_JOB_BACKOFF_BASE_S", "600")
)
BACKOFF_MAX_SECONDS: float = float(
    os.environ.get("INGEST_JOB_BACKOFF_MAX_S", "3600")
)


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do with a job that has just failed.

    `status` is what the row becomes. `next_attempt_at` is set only when the
    job is going back on the queue, and is what stops a requeued job being
    claimed again immediately -- without it, "retry" would mean "spin".
    """

    status: str
    attempt_count: int
    next_attempt_at: datetime | None

    @property
    def will_retry(self) -> bool:
        return self.status == "QUEUED"


def backoff_seconds(attempt_count: int) -> float:
    """Delay before attempt number `attempt_count + 1`.

    Exponential from the base, capped. `attempt_count` is the number of
    attempts *already* made, so the first retry waits exactly the base.
    """
    if attempt_count < 1:
        return BACKOFF_BASE_SECONDS
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt_count - 1)), BACKOFF_MAX_SECONDS)


def is_transient(failure_kind: str | None) -> bool:
    return failure_kind in TRANSIENT_KINDS


def decide(
    *,
    failure_kind: str | None,
    attempt_count: int,
    now: datetime | None = None,
) -> Decision:
    """Decide the fate of a job that has just failed.

    `attempt_count` is the value already on the row -- the attempts made
    *before* this one -- so the attempt that just failed is `attempt_count + 1`.

    Three outcomes:

    * **QUEUED** with a `next_attempt_at` -- a transient failure with budget
      left. The row goes back on the queue and is invisible to `claim_next_job`
      until the backoff elapses.
    * **DEAD_LETTER** -- a transient failure that has used its budget. It kept
      failing for a reason that usually passes, and we stopped trying.
    * **FAILED** -- a terminal failure. Retrying a checksum mismatch or a model
      that rejects the content produces the identical failure and spends real
      tokens doing it.
    """
    attempts_made = attempt_count + 1

    if not is_transient(failure_kind):
        return Decision("FAILED", attempts_made, None)

    if attempts_made >= MAX_ATTEMPTS:
        return Decision("DEAD_LETTER", attempts_made, None)

    if failure_kind in IMMEDIATE_KINDS:
        return Decision("QUEUED", attempts_made, None)

    moment = now or datetime.now(UTC)
    return Decision(
        "QUEUED",
        attempts_made,
        moment + timedelta(seconds=backoff_seconds(attempts_made)),
    )

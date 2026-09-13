"""Per-user and per-IP request limits, counted in the database.

## Why the database

A counter in a process dict is per-instance. Two API instances behind a load
balancer each enforce the limit independently, so the real limit is twice
what it says; a restart forgets that anyone was ever throttled. This project
has already fixed that exact defect twice -- ticket idempotency and crawl
discovery review, both moved into the database by P2-5 -- and a limiter with
those properties is not a limit.

The counter is a fixed window: one row per (key, window start), incremented
by a single atomic statement. Fixed windows allow a burst of up to twice the
limit across a window boundary, which a sliding window would not; that is
accepted deliberately, because the thing being protected is a daily token
budget and an hourly cost, not a fragile downstream service. The simpler
mechanism is the one that stays correct under concurrency, and correctness
under concurrency is the whole reason this is not a dict.

## What is limited

`login` is keyed on the client address **and** on the email being attempted,
because it is the one limit that must work before anyone is identified. Every
other limit is keyed on the authenticated user id, which is exact: per-IP
limiting punishes an office behind one NAT and does nothing against a caller
spread across many addresses.

The chat limit is the one that matters most. Every turn spends Groq tokens
against a daily budget that this project's own testing exhausted repeatedly
with a single developer. Authentication bounds *who* can spend it; this
bounds how much any one of them can.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DISABLE_ENV: Final[str] = "RATE_LIMIT_DISABLED"


@dataclass(frozen=True)
class Limit:
    """`max_events` per `window`, under a named scope."""

    scope: str
    max_events: int
    window: timedelta

    def bucket_key(self, principal: str) -> str:
        return f"{self.scope}:{principal}"

    def window_start(self, now: datetime) -> datetime:
        """Floor `now` to the start of its window.

        Deriving the window from the clock rather than from the first request
        keeps the key computable by every instance without coordination.
        """
        seconds = int(self.window.total_seconds())
        epoch = int(now.timestamp())
        return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


# Credential stuffing. Both keys are enforced: per-IP stops one host working
# through an address list, per-email stops a botnet working through passwords
# for one account.
LOGIN_PER_IP = Limit("login-ip", 5, timedelta(minutes=15))
LOGIN_PER_EMAIL = Limit("login-email", 5, timedelta(minutes=15))

def _int_env(name: str, default: int) -> int:
    """An integer setting, falling back rather than crashing on nonsense.

    A malformed limit should not stop the process booting: the default is
    safe, and refusing to start over a typo in an optional tuning knob trades
    a small misconfiguration for a total outage.
    """
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Every chat turn costs Groq tokens.
#
# Keyed per *caller* -- an account id when someone is signed in, an IP address
# when they are not. An IP is a much weaker identity than an account: shared
# behind NAT, and rotated with a VPN in seconds. These bound an ordinary user;
# they do not bound a determined one.
CHAT_PER_MINUTE = Limit("chat-minute", 20, timedelta(minutes=1))
CHAT_PER_DAY = Limit("chat-day", 500, timedelta(days=1))

# The limit that actually protects the deployment, and the reason it exists.
#
# Chat is public: no account, no signup, nothing between the internet and the
# model budget. One measured turn costs **6,871 tokens** against a free tier
# allowing 8,000 a minute, so a single caller rotating addresses can drain a
# day's budget in minutes and the per-IP limits above would each read as
# untouched while it happened.
#
# So this one counts *every* turn into one bucket regardless of who asked. It
# is a spend ceiling rather than an abuse limit: when it trips, the assistant
# says it is busy and stops, which is a bad afternoon instead of an exhausted
# quota and a week of failed answers. `CHAT_GLOBAL_PER_DAY` tunes it -- raise
# it the moment the deployment stops being on a free tier.
CHAT_GLOBAL_PER_DAY = Limit(
    "chat-global-day", _int_env("CHAT_GLOBAL_PER_DAY", 400), timedelta(days=1)
)

# Crawls are expensive and noisy for whoever is on the other end.
INGEST_PER_MINUTE = Limit("ingest-minute", 10, timedelta(minutes=1))


class RateLimitExceeded(Exception):
    """The caller has used up a limit. The API turns this into a 429."""

    def __init__(self, limit: Limit, retry_after: int) -> None:
        super().__init__(
            f"Rate limit exceeded: at most {limit.max_events} per "
            f"{_describe(limit.window)}."
        )
        self.limit = limit
        self.retry_after = retry_after


def _describe(window: timedelta) -> str:
    seconds = int(window.total_seconds())
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            count = seconds // size
            return name if count == 1 else f"{count} {name}s"
    return f"{seconds} seconds"


def rate_limiting_disabled() -> bool:
    """Escape hatch for tests and for local single-user development.

    Read per call rather than cached, so a test can set it with monkeypatch
    without having to reach into module state.
    """
    return os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


async def _increment(
    session: AsyncSession, key: str, window_start: datetime
) -> int:
    """Increment one bucket and return its new count, atomically.

    `ON CONFLICT DO UPDATE ... RETURNING` is what makes this safe under
    concurrency: two requests cannot both read 4 and both write 5. A
    read-then-write in Python would have exactly that race, and it would only
    show up under the load the limiter exists for.
    """
    from db.models import RateLimitBucket

    statement = (
        pg_insert(RateLimitBucket)
        .values(key=key, window_start=window_start, count=1)
        .on_conflict_do_update(
            index_elements=["key", "window_start"],
            set_={"count": RateLimitBucket.count + 1},
        )
        .returning(RateLimitBucket.count)
    )
    result = await session.execute(statement)
    return int(result.scalar_one())


async def _increment_portable(
    session: AsyncSession, key: str, window_start: datetime
) -> int:
    """The same increment for backends without `ON CONFLICT ... RETURNING`.

    Used only by the test suite, which runs against SQLite. It has the race
    the Postgres path does not, which is acceptable in a single-threaded test
    and would not be in production -- hence two implementations rather than
    one lowest-common-denominator one.
    """
    from db.models import RateLimitBucket

    updated = await session.execute(
        update(RateLimitBucket)
        .where(
            RateLimitBucket.key == key,
            RateLimitBucket.window_start == window_start,
        )
        .values(count=RateLimitBucket.count + 1)
    )
    if updated.rowcount:
        current = await session.execute(
            select(RateLimitBucket.count).where(
                RateLimitBucket.key == key,
                RateLimitBucket.window_start == window_start,
            )
        )
        return int(current.scalar_one())

    session.add(RateLimitBucket(key=key, window_start=window_start, count=1))
    await session.flush()
    return 1


def _dialect_of(session: AsyncSession) -> str:
    """The backend behind this session, defaulting to the production one.

    An unknown backend must not silently pick the portable path: that path
    has a read-then-write race, and taking it in production would make the
    limiter approximate under exactly the load it exists for.
    """
    for source in (getattr(session, "bind", None), None):
        bind = source if source is not None else _safe_get_bind(session)
        dialect = getattr(bind, "dialect", None)
        name = getattr(dialect, "name", None)
        if name:
            return name
    return "postgresql"


def _safe_get_bind(session: AsyncSession):
    try:
        return session.get_bind()
    except Exception:  # no bind configured (a hand-rolled fake in a test)
        return None


async def check(
    session: AsyncSession,
    limit: Limit,
    principal: str,
    *,
    now: datetime | None = None,
) -> int:
    """Count one event against `limit`, raising once the limit is passed.

    Returns the count so far, so a caller can log how close someone is.
    """
    if rate_limiting_disabled():
        return 0

    moment = now or datetime.now(timezone.utc)
    window_start = limit.window_start(moment)
    key = limit.bucket_key(principal)

    increment = _increment if _dialect_of(session) == "postgresql" else _increment_portable
    count = await increment(session, key, window_start)

    if count > limit.max_events:
        retry_after = int((window_start + limit.window - moment).total_seconds())
        logger.info(
            "Rate limit %s hit by %s (%d/%d).",
            limit.scope,
            principal,
            count,
            limit.max_events,
        )
        raise RateLimitExceeded(limit, max(retry_after, 1))
    return count


async def sweep_expired(session: AsyncSession, *, older_than: timedelta) -> int:
    """Delete buckets whose window closed long ago. Returns rows removed.

    Called opportunistically rather than on a schedule: the table is small,
    and a sweeper process would be more machinery than the problem deserves
    -- the same judgement `crawl_discovery` already makes.
    """
    from db.models import RateLimitBucket

    cutoff = datetime.now(timezone.utc) - older_than
    result = await session.execute(
        RateLimitBucket.__table__.delete().where(
            RateLimitBucket.window_start < cutoff
        )
    )
    return int(result.rowcount or 0)

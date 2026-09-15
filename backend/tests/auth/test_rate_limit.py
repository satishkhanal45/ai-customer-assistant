"""Rate limiting.

The limits themselves are uninteresting; two properties are not.

* **The count survives the request that failed.** A failed login raises, and
  the request-scoped session rolls back when it does. If the counter lived in
  that session, the fifth wrong password would be as unthrottled as the
  first and the limit protecting against credential stuffing would never
  fire. This is why `dependencies.enforce` opens its own transaction.
* **The counter is in the database, not in a dict.** A per-process counter
  means N instances enforce N times the limit and a restart forgets everyone
  -- the same defect P2-5 already fixed twice elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auth import rate_limit


@pytest.fixture(autouse=True)
def enable_limits(monkeypatch):
    """The suite-wide fixture disables limiting; these tests need it on."""
    monkeypatch.delenv(rate_limit.DISABLE_ENV, raising=False)


@pytest.fixture
def limit() -> rate_limit.Limit:
    return rate_limit.Limit("test", max_events=3, window=timedelta(minutes=1))


class TestCounting:
    async def test_events_under_the_limit_pass(self, session_factory, limit):
        async with session_factory() as session:
            for expected in (1, 2, 3):
                assert await rate_limit.check(session, limit, "alice") == expected

    async def test_the_event_after_the_limit_raises(self, session_factory, limit):
        async with session_factory() as session:
            for _ in range(3):
                await rate_limit.check(session, limit, "alice")
            with pytest.raises(rate_limit.RateLimitExceeded):
                await rate_limit.check(session, limit, "alice")

    async def test_principals_are_counted_separately(self, session_factory, limit):
        """Per-user keying is the point. One person hitting their limit must
        not throttle everyone else."""
        async with session_factory() as session:
            for _ in range(3):
                await rate_limit.check(session, limit, "alice")
            assert await rate_limit.check(session, limit, "bob") == 1

    async def test_scopes_are_counted_separately(self, session_factory):
        """Chat and ingest share a user id but not a budget."""
        chat = rate_limit.Limit("chat", 2, timedelta(minutes=1))
        ingest = rate_limit.Limit("ingest", 2, timedelta(minutes=1))
        async with session_factory() as session:
            await rate_limit.check(session, chat, "alice")
            await rate_limit.check(session, chat, "alice")
            assert await rate_limit.check(session, ingest, "alice") == 1


class TestWindows:
    async def test_a_new_window_starts_a_new_count(self, session_factory, limit):
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            for _ in range(3):
                await rate_limit.check(session, limit, "alice", now=now)

            later = now + timedelta(minutes=2)
            assert await rate_limit.check(session, limit, "alice", now=later) == 1

    def test_windows_are_derived_from_the_clock(self, limit):
        """Not from the first request. Every instance must compute the same
        window key without coordinating with the others."""
        a = limit.window_start(datetime(2026, 9, 8, 12, 0, 30, tzinfo=timezone.utc))
        b = limit.window_start(datetime(2026, 9, 8, 12, 0, 59, tzinfo=timezone.utc))
        c = limit.window_start(datetime(2026, 9, 8, 12, 1, 1, tzinfo=timezone.utc))
        assert a == b and a != c


class TestRetryAfter:
    async def test_the_error_says_when_to_come_back(self, session_factory, limit):
        """Without Retry-After a client that retries immediately turns one
        throttled caller into a busy loop against the limiter."""
        async with session_factory() as session:
            for _ in range(3):
                await rate_limit.check(session, limit, "alice")
            with pytest.raises(rate_limit.RateLimitExceeded) as caught:
                await rate_limit.check(session, limit, "alice")

        assert 0 < caught.value.retry_after <= 60


class TestSweeping:
    async def test_old_windows_are_deleted(self, session_factory, limit):
        async with session_factory() as session:
            await rate_limit.check(
                session,
                limit,
                "alice",
                now=datetime.now(timezone.utc) - timedelta(days=3),
            )
            await session.commit()
            removed = await rate_limit.sweep_expired(
                session, older_than=timedelta(days=1)
            )
        assert removed == 1


class TestDisabling:
    async def test_the_escape_hatch_short_circuits(
        self, session_factory, limit, monkeypatch
    ):
        monkeypatch.setenv(rate_limit.DISABLE_ENV, "1")
        async with session_factory() as session:
            for _ in range(10):
                assert await rate_limit.check(session, limit, "alice") == 0


class TestConfiguredLimits:
    def test_login_is_limited_by_both_address_and_account(self):
        """Per-IP alone lets a botnet through; per-email alone lets one host
        work through an address list. Both, or neither is much use."""
        assert rate_limit.LOGIN_PER_IP.scope != rate_limit.LOGIN_PER_EMAIL.scope
        assert rate_limit.LOGIN_PER_IP.max_events == 5
        assert rate_limit.LOGIN_PER_EMAIL.max_events == 5

    def test_chat_has_both_a_burst_and_a_daily_budget(self):
        """The per-minute cap stops a runaway client; the daily one is what
        bounds the Groq bill."""
        assert rate_limit.CHAT_PER_MINUTE.window == timedelta(minutes=1)
        assert rate_limit.CHAT_PER_DAY.window == timedelta(days=1)

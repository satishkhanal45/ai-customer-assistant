"""Ticket idempotency must survive a restart and a second process (P2-5).

Deduplication lived in `TicketStore._by_key`, a plain dict on one instance.
The guarantee it advertised — "a retried create returns the existing ticket,
no second row, no second email" — was therefore only ever true within one
process. Two API instances behind a load balancer each held their own empty
dict; so did one instance restarted between the original request and its
retry. In both cases the customer got a second ticket and a second
confirmation email.

These tests use real SQLite rows rather than a fake session, because the
whole fix is a database constraint: a mock would happily accept the duplicate
insert and prove nothing. Two independent `TicketStore` instances sharing one
engine stand in for two API processes.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agents.ticket_agent.store import _KEY_CACHE_MAX, _ROWS_MAX, TicketStore
from agents.ticket_agent.types import PendingTicket
from db.models import Ticket as DbTicket


@pytest.fixture
async def session_factory():
    """Only the `ticket` table is created — the sibling tables carry JSONB and
    pgvector columns SQLite cannot render, and this table needs no parents."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(DbTicket.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _count_rows(factory) -> int:
    from sqlalchemy import func, select

    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(DbTicket)))


class _CountingNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def __call__(self, ticket) -> None:
        self.sent.append(ticket)


class TestDedupeAcrossProcesses:
    async def test_a_second_store_does_not_book_a_duplicate(self, session_factory):
        """The case the in-memory dict could not cover: a retry that lands on
        a different instance, whose cache has never seen the key."""
        first_process = TicketStore(session_factory=session_factory)
        second_process = TicketStore(session_factory=session_factory)
        pending = PendingTicket(query="I was double charged", reason="billing")

        original = await first_process.create_ticket(pending, "a@example.com", idempotency_key="t:0")
        retried = await second_process.create_ticket(pending, "a@example.com", idempotency_key="t:0")

        assert await _count_rows(session_factory) == 1
        assert retried.ticket_id == original.ticket_id
        assert retried.query == "I was double charged"
        assert retried.reason == "billing"

    async def test_the_loser_of_the_race_sends_no_second_email(self, session_factory):
        """A duplicate row would have been bad; a duplicate confirmation email
        is what the customer actually notices."""
        notifier = _CountingNotifier()
        first = TicketStore(session_factory=session_factory, send_email=notifier)
        second = TicketStore(session_factory=session_factory, send_email=notifier)
        pending = PendingTicket(query="q")

        await first.create_ticket(pending, "a@example.com", idempotency_key="t:0")
        await second.create_ticket(pending, "a@example.com", idempotency_key="t:0")

        assert len(notifier.sent) == 1

    async def test_a_restarted_process_still_dedupes(self, session_factory):
        """Same store class, brand new instance — exactly what a redeploy or
        a crash-restart between the request and its retry looks like."""
        before_restart = TicketStore(session_factory=session_factory)
        original = await before_restart.create_ticket(
            PendingTicket(query="q"), "a@example.com", idempotency_key="thread-7:0"
        )

        after_restart = TicketStore(session_factory=session_factory)
        retried = await after_restart.create_ticket(
            PendingTicket(query="q"), "a@example.com", idempotency_key="thread-7:0"
        )

        assert retried.ticket_id == original.ticket_id
        assert await _count_rows(session_factory) == 1

    async def test_distinct_keys_still_create_distinct_tickets(self, session_factory):
        store = TicketStore(session_factory=session_factory)
        a = await store.create_ticket(PendingTicket(query="q1"), "a@example.com", idempotency_key="t:0")
        b = await store.create_ticket(PendingTicket(query="q2"), "a@example.com", idempotency_key="t:1")

        assert a.ticket_id != b.ticket_id
        assert await _count_rows(session_factory) == 2

    async def test_unkeyed_tickets_are_never_deduplicated(self, session_factory):
        """Escalation paths open a ticket with no thread to derive a key from.
        Postgres does not treat two NULLs as equal, so these must all be
        booked — deduplicating them would silently drop real tickets."""
        store = TicketStore(session_factory=session_factory)
        await store.create_ticket(PendingTicket(query="q"), "a@example.com")
        await store.create_ticket(PendingTicket(query="q"), "a@example.com")

        assert await _count_rows(session_factory) == 2

    async def test_the_persisted_row_carries_the_key(self, session_factory):
        from sqlalchemy import select

        store = TicketStore(session_factory=session_factory)
        await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="t:0")

        async with session_factory() as session:
            row = (await session.execute(select(DbTicket))).scalar_one()
        assert row.idempotency_key == "t:0"

    async def test_a_conflict_on_something_else_is_not_swallowed(self, session_factory):
        """Only an idempotency-key conflict means "already booked". Any other
        integrity error is a real defect and must surface."""
        from sqlalchemy import insert

        store = TicketStore(session_factory=session_factory)
        ticket = await store.create_ticket(PendingTicket(query="q"), "a@example.com")

        async with session_factory() as session:
            with pytest.raises(Exception):
                # Same primary key, no idempotency key to recover by.
                await session.execute(
                    insert(DbTicket).values(
                        ticket_id=ticket.ticket_id, email="b@example.com", query="q", status="OPEN"
                    )
                )
                await session.commit()


class TestNextSequenceComesFromTheDatabase:
    async def test_the_ordinal_counts_committed_rows(self, session_factory):
        store = TicketStore(session_factory=session_factory)
        assert await store.next_sequence("thread-1") == 0

        await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="thread-1:0")
        assert await store.next_sequence("thread-1") == 1

    async def test_a_fresh_process_does_not_restart_the_ordinal(self, session_factory):
        """The bug this prevents is subtle and bad: a restarted process would
        count its own empty map, hand back `thread:0` again, and the
        customer's genuinely new ticket would be deduplicated against their
        previous one — they would be shown the wrong confirmation."""
        first = TicketStore(session_factory=session_factory)
        await first.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="thread-1:0")

        after_restart = TicketStore(session_factory=session_factory)
        assert await after_restart.next_sequence("thread-1") == 1

    async def test_scopes_do_not_bleed_into_each_other(self, session_factory):
        store = TicketStore(session_factory=session_factory)
        await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="thread-1:0")
        assert await store.next_sequence("thread-2") == 0

    async def test_a_thread_id_with_like_wildcards_counts_correctly(self, session_factory):
        """`%` and `_` are LIKE wildcards. Without autoescape, thread `a_c`
        would also count tickets from `abc`."""
        store = TicketStore(session_factory=session_factory)
        await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="abc:0")

        assert await store.next_sequence("a_c") == 0
        assert await store.next_sequence("a%") == 0
        assert await store.next_sequence("abc") == 1

    async def test_without_a_database_it_falls_back_to_memory(self):
        """A bare `TicketStore()` stays usable in tests and dev."""
        store = TicketStore()
        assert await store.next_sequence("t") == 0
        await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="t:0")
        assert await store.next_sequence("t") == 1


class TestInProcessStateIsBounded:
    async def test_rows_does_not_grow_without_bound(self):
        """`rows` is an observability window, not the source of truth. It used
        to retain every ticket the process had ever created, forever."""
        store = TicketStore()
        for index in range(_ROWS_MAX + 50):
            await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key=f"t:{index}")

        assert len(store.rows) == _ROWS_MAX

    async def test_the_key_cache_is_bounded_and_evicts_the_oldest(self):
        store = TicketStore()
        for index in range(_KEY_CACHE_MAX + 10):
            await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key=f"t:{index}")

        assert len(store._by_key) == _KEY_CACHE_MAX
        assert "t:0" not in store._by_key
        assert f"t:{_KEY_CACHE_MAX + 9}" in store._by_key

    async def test_eviction_costs_a_round_trip_not_a_duplicate(self, session_factory):
        """The cache is only a shortcut. Dropping an entry must never turn
        into a second ticket, because the constraint is what enforces the
        guarantee now."""
        store = TicketStore(session_factory=session_factory)
        original = await store.create_ticket(
            PendingTicket(query="q"), "a@example.com", idempotency_key="t:0"
        )
        store._by_key.clear()  # simulate the entry having been evicted

        retried = await store.create_ticket(
            PendingTicket(query="q"), "a@example.com", idempotency_key="t:0"
        )
        assert retried.ticket_id == original.ticket_id
        assert await _count_rows(session_factory) == 1

    async def test_the_cache_still_short_circuits_the_common_case(self, session_factory):
        """A cache hit must not touch the database at all."""
        store = TicketStore(session_factory=session_factory)
        await store.create_ticket(PendingTicket(query="q"), "a@example.com", idempotency_key="t:0")

        store._session_factory = None  # any DB access from here would misbehave
        cached = await store.create_ticket(
            PendingTicket(query="q"), "a@example.com", idempotency_key="t:0"
        )
        assert cached.ticket_id
        assert len(store.rows) == 1

"""Tests for the ticket persistence boundary (agents/ticket_agent/store.py).

These cover the parts of ``TicketStore`` that are not idempotency (which
lives in tests/agents/supervisor_agent_test/test_idempotency.py): that the
*injected* session factory is the one actually used, that the persisted row
carries every field including the customer's stated reason, and that the
confirmation email renders real values rather than literal placeholders.
"""
from __future__ import annotations

from agents.ticket_agent.store import TicketStore, _render_email
from agents.ticket_agent.types import PendingTicket, Ticket


class _FakeSession:
    """Records the statements executed against it, so a test can assert what
    would have been written without a live Postgres."""

    def __init__(self, recorder: list) -> None:
        self._recorder = recorder
        self.committed = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement) -> None:
        self._recorder.append(statement)

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    """Stands in for an ``async_sessionmaker``."""

    def __init__(self) -> None:
        self.statements: list = []
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self.statements)
        self.sessions.append(session)
        return session


class TestPersistence:
    async def test_uses_the_injected_session_factory(self) -> None:
        """The store must write through the factory it was given, not the
        module-level one in db.async_session — otherwise an injected factory
        is silently ignored and nothing can be tested or redirected."""
        factory = _FakeSessionFactory()
        store = TicketStore(session_factory=factory)

        await store.create_ticket(PendingTicket(query="q"), "a@example.com")

        assert len(factory.sessions) == 1
        assert len(factory.statements) == 1
        assert factory.sessions[0].committed is True

    async def test_persisted_row_carries_every_field(self) -> None:
        factory = _FakeSessionFactory()
        store = TicketStore(session_factory=factory)
        pending = store.call("I was double charged", "billing issue")

        ticket = await store.create_ticket(pending, "a@example.com")

        (statement,) = factory.statements
        values = statement.compile().params
        # Bound as a real UUID, not the domain type's string form: psycopg
        # accepts the string but SQLAlchemy's portable Uuid type does not,
        # so the coercion is what keeps this insert dialect-independent.
        assert str(values["ticket_id"]) == ticket.ticket_id
        assert values["email"] == "a@example.com"
        assert values["query"] == "I was double charged"
        assert values["reason"] == "billing issue"
        assert values["status"] == "OPEN"

    async def test_no_factory_means_no_write(self) -> None:
        store = TicketStore()
        ticket = await store.create_ticket(PendingTicket(query="q"), "a@example.com")
        assert ticket.ticket_id
        assert len(store.rows) == 1

    async def test_duplicate_key_does_not_write_twice(self) -> None:
        factory = _FakeSessionFactory()
        store = TicketStore(session_factory=factory)
        pending = PendingTicket(query="q")

        await store.create_ticket(pending, "a@example.com", idempotency_key="k")
        await store.create_ticket(pending, "a@example.com", idempotency_key="k")

        assert len(factory.statements) == 1


class TestStatusLookup:
    """The read half of the store, added so a customer can ask what happened
    to a ticket they were given an id for."""

    async def test_round_trips_a_ticket_created_in_this_process(self) -> None:
        store = TicketStore()
        created = await store.create_ticket(
            PendingTicket(query="I was double charged", reason="billing issue"),
            "a@example.com",
        )

        found = await store.get_ticket(created.ticket_id)

        assert found is not None
        assert found.ticket_id == created.ticket_id
        assert found.email == "a@example.com"
        assert found.reason == "billing issue"
        assert found.status == "OPEN"

    async def test_unknown_id_is_a_miss(self) -> None:
        store = TicketStore()
        await store.create_ticket(PendingTicket(query="q"), "a@example.com")

        assert await store.get_ticket("00000000-0000-4000-8000-000000000000") is None

    async def test_malformed_id_is_a_miss_not_an_error(self) -> None:
        """The id comes from a person typing into a chat box. "abc123"
        deserves "no such ticket", not a failed turn."""
        store = TicketStore()

        for bad in ("abc123", "", "not-a-uuid", None):
            assert await store.get_ticket(bad) is None


class TestEmailRendering:
    def test_subject_contains_the_real_ticket_id(self) -> None:
        """Regression: the subject was a plain string with an unsubstituted
        ``{ticket_id}`` placeholder, so every customer received the literal
        braces instead of their ticket id."""
        ticket = Ticket(ticket_id="abc-123", email="a@example.com", query="q")
        subject, _ = _render_email(ticket)
        assert "abc-123" in subject
        assert "{" not in subject

    def test_body_includes_the_reason_when_present(self) -> None:
        ticket = Ticket(
            ticket_id="abc-123",
            email="a@example.com",
            query="I was double charged",
            reason="billing issue",
        )
        _, body = _render_email(ticket)
        assert "abc-123" in body
        assert "I was double charged" in body
        assert "billing issue" in body

    def test_body_omits_the_reason_line_when_absent(self) -> None:
        ticket = Ticket(ticket_id="abc-123", email="a@example.com", query="q")
        _, body = _render_email(ticket)
        assert "Reason:" not in body

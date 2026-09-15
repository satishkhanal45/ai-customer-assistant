"""
Idempotent ticket persistence boundary for the Supervisor adapter.

``create_ticket`` itself is pure and immutable (see ticket_agent.py) — it
cannot dedupe because it has no memory. Deduplication lives here instead, in
the one place the adapter hands a created ``Ticket`` to something stateful.
A retried create with the same ``idempotency_key`` returns the previously
created ``Ticket`` instead of booking a second one, so the caller renders the
*existing* ticket's confirmation rather than a fresh row.

**The database is the authority for that guarantee, not this object** (P2-5).
Deduplication used to live only in a Python dict on one ``TicketStore``
instance, which made the promise per-process: two API instances behind a load
balancer each held their own empty dict, so a retried request booked a second
ticket and sent the customer a second confirmation email. Restarting a single
instance between the original and the retry did the same thing. The
``uq_ticket_idempotency_key`` unique constraint (migration ``3d6f8b2c17ae``)
now enforces it where every instance can see it; the in-process map is kept
only as a bounded cache that saves a round trip on the common case.

The same reasoning applies to ``next_sequence``: the per-thread ordinal that
forms the server-derived key is counted in the ``ticket`` table, because a
count from a fresh process would restart at zero and silently dedupe a
genuinely new ticket against an old one.

Collaborators are injected, never constructed here — the same convention the
rest of this codebase follows, and the reason a bare ``TicketStore()`` is
inert: it dedupes in memory and does nothing else. The composition root
(``services.chat_service.build_chat_service``) is what wires the real
database session factory and the real email notifier in. This keeps tests
from silently opening SMTP connections or touching Postgres just by
constructing a store.

``create_ticket`` is a coroutine because persisting a ticket is real async
database I/O (``AsyncSession``). The Supervisor's ticket adapter node is
therefore async too, which means the compiled Supervisor graph must be
driven with ``ainvoke`` — already true in production, since the Knowledge
Agent's adapter node is async for the same reason.

SMTP settings are read from ``os.environ`` when a mail is actually sent. This
module does **not** load ``backend/.env`` — it used to call ``load_dotenv()``
at import scope, which mutated the whole process's environment as a side
effect of importing a ticket module and made unrelated code (the Knowledge
provider resolver) behave differently depending on import order. Loading
configuration belongs to the entry point; see ``config.load_env``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import uuid
from collections import OrderedDict, deque
from email.message import EmailMessage
from typing import Callable, Optional

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.ticket_agent.ticket_agent import call as _call
from agents.ticket_agent.ticket_agent import create_ticket as _build_ticket
from agents.ticket_agent.types import PendingTicket, Ticket

logger = logging.getLogger(__name__)

# Caps on the two in-process structures. Both used to grow without bound for
# the lifetime of the process: a long-running API instance accumulated every
# ticket it had ever created, in full, forever. Neither is load-bearing any
# more -- the database enforces idempotency -- so they can be small.
_KEY_CACHE_MAX = 512
_ROWS_MAX = 512

# What ``TicketStore`` calls after a ticket is booked. Sync by design: it is
# blocking SMTP I/O, and the store runs it off the event loop via
# ``asyncio.to_thread``.
EmailNotifier = Callable[[Ticket], None]


def _as_uuid(ticket_id: str) -> uuid.UUID:
    """Coerce the domain ticket id to a real ``UUID`` for the database.

    ``Ticket.ticket_id`` is a ``str`` (``str(uuid.uuid4())``) while
    ``ticket.ticket_id`` is a ``UUID`` column. psycopg happens to accept the
    string form, so Postgres never complained — but SQLAlchemy's portable
    ``Uuid`` implementation calls ``.hex`` on the bound value, so the same
    insert failed on any other dialect. Converting here keeps the domain type
    a plain string (nothing else wants a UUID object) without leaving a
    driver-specific coincidence load-bearing.
    """
    return ticket_id if isinstance(ticket_id, uuid.UUID) else uuid.UUID(str(ticket_id))


def _get_smtp_config() -> dict[str, str | int | None]:
    """Read SMTP configuration from environment variables.

    Returns a dict with keys: host, port, username, password, from_email.
    Any value may be None if the corresponding env var is not set.
    """
    return {
        "host": os.getenv("SMTP_HOST"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "username": os.getenv("SMTP_USER"),
        "password": os.getenv("SMTP_PASSWORD"),
        "from_email": os.getenv("EMAIL_FROM"),
    }


def _render_email(ticket: Ticket) -> tuple[str, str]:
    """Pure: render ``(subject, body)`` for a booked ticket.

    Split out from the sending path so the wording is unit-testable without
    an SMTP server — which is how the missing f-string that shipped a literal
    ``{ticket_id}`` to every customer went unnoticed.
    """
    subject = f"Your ticket has been created (ID: {ticket.ticket_id})"

    reason_line = f"Reason: {ticket.reason}\n" if ticket.reason else ""
    body = f"""Dear {ticket.email},

Your ticket has been successfully created.

────────────────────────────────────────────────────────
Ticket ID: {ticket.ticket_id}
Query: {ticket.query}
{reason_line}Status: We'll follow up with you at {ticket.email}.

What's next?
• Our support team will review your query
• We'll get back to you soon with a resolution or next steps
• You can reply to this email if you have any additional information

If you have any urgent questions, please contact our support team directly.

Thank you for reaching out!

Best regards,
The Support Team
────────────────────────────────────────────────────────
"""
    return subject, body


def send_ticket_email(ticket: Ticket) -> None:
    """Send a ticket-creation email via SMTP.

    Blocking. Never raises: a failed notification must not undo a ticket that
    is already persisted, so every error is logged and swallowed. When the
    SMTP environment block is incomplete this is a no-op, which is the normal
    state in local development.
    """
    config = _get_smtp_config()
    host = config.get("host")
    port = config.get("port")
    username = config.get("username")
    password = config.get("password")
    from_email = config.get("from_email")

    if not host or not port or not from_email:
        logger.debug("SMTP not configured; skipping notification for ticket %s", ticket.ticket_id)
        return

    subject, body = _render_email(ticket)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ticket.email
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
    except Exception:  # noqa: BLE001 - notification is best-effort by design
        logger.warning(
            "failed to send confirmation email for ticket %s", ticket.ticket_id, exc_info=True
        )


class TicketStore:
    """Handles idempotency and persistence for ticket creation.

    This is the full ``ticket_ops`` object the Supervisor adapter drives:
    ``call(query, reason)`` on the opening turn and ``create_ticket(...)`` on
    the resume turn, so a ``TicketStore`` instance can be passed directly as
    ``build_supervisor_graph(ticket_ops=...)``.

    ``create_ticket(pending, email, idempotency_key)``:

    - With an ``idempotency_key`` this process has already seen: returns the
      cached ``Ticket`` immediately — no round trip, no second email.
    - With a key *another* process has already used: the insert loses the
      race against ``uq_ticket_idempotency_key``, and the row that won is
      read back and returned. Still no second row and no second email — this
      is the case the in-memory map could not cover.
    - Otherwise: books the ticket, remembers it, and notifies the customer.

    ``next_sequence(scope)`` returns how many tickets already exist under an
    idempotency-key scope prefix (e.g. a ``thread_id``) — the server-derived
    part of the idempotency key (§2.3). It is a coroutine because that count
    comes from the database; a hand-rolled sync fake is still accepted by the
    adapter (see ``agents_wiring._idempotency_key``).

    ``rows`` is the observable "table" the adapter's persistence step and the
    tests assert against. It is bounded: it is an observability window, never
    the source of truth, and an unbounded one leaked memory for the lifetime
    of the process.
    """

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        send_email: Optional[EmailNotifier] = None,
    ) -> None:
        # A bounded LRU, not the guarantee. Evicting an entry costs one extra
        # database round trip on a retry, never a duplicate ticket.
        self._by_key: OrderedDict[str, Ticket] = OrderedDict()
        self.rows: deque[Ticket] = deque(maxlen=_ROWS_MAX)
        self._session_factory = session_factory
        self._send_email = send_email

    def call(self, query: str, reason: str | None = None) -> PendingTicket:
        """Open a ticket for ``query`` — compose so the store is a complete
        ``ticket_ops`` for the Supervisor adapter."""
        return _call(query, reason)

    async def next_sequence(self, scope: str) -> int:
        """Number of tickets already created under an idempotency-key
        ``scope`` prefix (for server-derived keys like ``thread_id``).

        Counted in the database rather than in memory. A fresh process
        counting its own empty map would restart the ordinal at zero and
        hand back a key an earlier ticket in the same thread already used —
        so the customer's genuinely new ticket would be deduplicated against
        their previous one and they would be shown the wrong confirmation.
        """
        if self._session_factory is None:
            return sum(1 for key in self._by_key if key.startswith(f"{scope}:"))

        from db.models import Ticket as _DbTicket

        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(_DbTicket)
                # autoescape, because a thread_id containing % or _ would
                # otherwise be read as LIKE wildcards and count the wrong rows.
                .where(_DbTicket.idempotency_key.startswith(f"{scope}:", autoescape=True))
            )
        return int(count or 0)

    async def create_ticket(
        self,
        pending: PendingTicket,
        email: str,
        idempotency_key: str | None = None,
    ) -> Ticket:
        if idempotency_key is not None:
            cached = self._by_key.get(idempotency_key)
            if cached is not None:
                self._by_key.move_to_end(idempotency_key)
                return cached

        ticket = _build_ticket(pending, email)
        stored, is_new = await self._persist(ticket, idempotency_key)
        self._remember(idempotency_key, stored, is_new=is_new)
        if is_new:
            # Only the writer notifies. Whoever lost the race must not send a
            # second confirmation email for a ticket that already exists.
            await self._notify(stored)
        return stored

    def _remember(self, idempotency_key: str | None, ticket: Ticket, *, is_new: bool) -> None:
        if is_new:
            self.rows.append(ticket)
        if idempotency_key is None:
            return
        self._by_key[idempotency_key] = ticket
        self._by_key.move_to_end(idempotency_key)
        while len(self._by_key) > _KEY_CACHE_MAX:
            self._by_key.popitem(last=False)

    async def _persist(
        self, ticket: Ticket, idempotency_key: str | None
    ) -> tuple[Ticket, bool]:
        """Insert the ticket through the *injected* session factory.

        Returns ``(ticket, is_new)``. ``is_new`` is False when a row with this
        idempotency key already existed, in which case the returned ``Ticket``
        is rebuilt from *that* row — the one the customer was already told
        about — rather than the one this call constructed.

        Uses ``self._session_factory`` rather than reaching for the
        module-level factory in ``db.async_session``, so a caller (or a test)
        that injects a factory actually gets the one it passed in.

        The conflict is caught rather than expressed as ``ON CONFLICT``: the
        insert then stays dialect-neutral, so this path behaves identically on
        the SQLite session the tests use and on Postgres in production. The
        constraint does the work either way.
        """
        if self._session_factory is None:
            return ticket, True

        from db.models import Ticket as _DbTicket

        async with self._session_factory() as session:
            try:
                await session.execute(
                    insert(_DbTicket).values(
                        ticket_id=_as_uuid(ticket.ticket_id),
                        email=ticket.email,
                        query=ticket.query,
                        reason=ticket.reason,
                        idempotency_key=idempotency_key,
                        priority=ticket.priority,
                        status=ticket.status,
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self._load_by_key(session, idempotency_key)
                if existing is None:
                    # The conflict was on something other than the idempotency
                    # key (a duplicate ticket_id, say). Nothing to hand back,
                    # and swallowing it would hide a real defect.
                    raise
                logger.info(
                    "ticket for idempotency_key %s already existed; returning it",
                    idempotency_key,
                )
                return existing, False

        return ticket, True

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        """Look one ticket up by id. ``None`` when there is no such row.

        The read half of this store. Creation has existed since the first
        milestone; status lookup answered "not available yet" from a
        hardcoded string in the router, so a customer who had just been given
        a ticket id could not ask what had become of it.

        A malformed id is a miss, not an error: the id arrives from a person
        typing it into a chat box, and "no ticket with that id" is the honest
        answer to "abc123" as much as to a well-formed uuid that does not
        exist. Raising would turn a typo into a failed turn.
        """
        try:
            key = _as_uuid(ticket_id)
        except (ValueError, AttributeError, TypeError):
            return None

        # No database configured: fall back to what this process has created,
        # the same way next_sequence does. Keeps the in-memory store usable
        # in tests and dev without a second code path in the caller.
        if self._session_factory is None:
            # `rows` is the ledger of everything created here; `_by_key` only
            # holds tickets that carried an idempotency key, so searching it
            # alone would miss any ticket created without one.
            return next(
                (t for t in self.rows if str(t.ticket_id) == str(key)), None
            )

        from db.models import Ticket as _DbTicket

        async with self._session_factory() as session:
            row = await session.get(_DbTicket, key)

        if row is None:
            return None
        return Ticket(
            ticket_id=str(row.ticket_id),
            email=row.email,
            query=row.query,
            reason=row.reason,
            priority=row.priority,
            status=row.status,
        )

    @staticmethod
    async def _load_by_key(session: AsyncSession, idempotency_key: str | None) -> Ticket | None:
        """Rebuild the domain ``Ticket`` for an existing idempotency key."""
        if idempotency_key is None:
            return None

        from db.models import Ticket as _DbTicket

        row = (
            await session.execute(
                select(_DbTicket).where(_DbTicket.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return Ticket(
            ticket_id=str(row.ticket_id),
            email=row.email,
            query=row.query,
            reason=row.reason,
            priority=row.priority,
            status=row.status,
        )

    async def _notify(self, ticket: Ticket) -> None:
        """Run the blocking SMTP send on a worker thread.

        Sending inline would block the event loop for the full SMTP round
        trip (up to the 10s socket timeout), stalling every other request in
        the process — the same mistake the ingestion pipeline already avoids
        with ``asyncio.to_thread``.
        """
        if self._send_email is None:
            return
        await asyncio.to_thread(self._send_email, ticket)

"""
Idempotent ticket persistence boundary for the Supervisor adapter.

``create_ticket`` itself is pure and immutable (see ticket_agent.py) — it
cannot dedupe because it has no memory. Deduplication lives here instead, in
the one place the adapter hands a created ``Ticket`` to something stateful.
A ``TicketStore`` records every ``(idempotency_key -> Ticket)`` it has
produced; a retried create with the same key returns the previously created
``Ticket`` instead of booking a second one, so the caller renders the
*existing* ticket's confirmation rather than a fresh row.

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
from email.message import EmailMessage
from typing import Callable, Optional

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.ticket_agent.ticket_agent import call as _call
from agents.ticket_agent.ticket_agent import create_ticket as _build_ticket
from agents.ticket_agent.types import PendingTicket, Ticket

logger = logging.getLogger(__name__)

# What ``TicketStore`` calls after a ticket is booked. Sync by design: it is
# blocking SMTP I/O, and the store runs it off the event loop via
# ``asyncio.to_thread``.
EmailNotifier = Callable[[Ticket], None]


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

    - With an ``idempotency_key`` already seen: returns the previously
      created ``Ticket`` — no second row, no second email, no state change.
    - Otherwise: builds a fresh ``Ticket``, records it in memory keyed by
      ``idempotency_key`` when supplied, appends to ``rows`` so tests can
      count created rows, inserts into the ``ticket`` table when a
      ``session_factory`` was injected, and notifies the customer when an
      ``send_email`` notifier was injected.

    ``next_sequence(scope)`` returns the count of tickets already recorded
    under an idempotency-key scope prefix (e.g. a ``thread_id``) — the
    server-derived part of the idempotency key (§2.3).

    ``rows`` is the observable "table" the adapter's persistence step and
    the tests assert against.
    """

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        send_email: Optional[EmailNotifier] = None,
    ) -> None:
        self._by_key: dict[str, Ticket] = {}
        self.rows: list[Ticket] = []
        self._session_factory = session_factory
        self._send_email = send_email

    def call(self, query: str, reason: str | None = None) -> PendingTicket:
        """Open a ticket for ``query`` — compose so the store is a complete
        ``ticket_ops`` for the Supervisor adapter."""
        return _call(query, reason)

    def next_sequence(self, scope: str) -> int:
        """Number of tickets already created under an idempotency-key
        ``scope`` prefix (for server-derived keys like ``thread_id``)."""
        return sum(1 for key in self._by_key if key.startswith(f"{scope}:"))

    async def create_ticket(
        self,
        pending: PendingTicket,
        email: str,
        idempotency_key: str | None = None,
    ) -> Ticket:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]

        ticket = _build_ticket(pending, email)
        self.rows.append(ticket)
        if idempotency_key is not None:
            self._by_key[idempotency_key] = ticket

        await self._persist(ticket)
        await self._notify(ticket)
        return ticket

    async def _persist(self, ticket: Ticket) -> None:
        """Insert the ticket through the *injected* session factory.

        Uses ``self._session_factory`` rather than reaching for the module-level
        factory in ``db.async_session``, so a caller (or a test) that injects a
        factory actually gets the one it passed in.
        """
        if self._session_factory is None:
            return

        from db.models import Ticket as _DbTicket

        async with self._session_factory() as session:
            await session.execute(
                insert(_DbTicket).values(
                    ticket_id=ticket.ticket_id,
                    email=ticket.email,
                    query=ticket.query,
                    reason=ticket.reason,
                    priority=ticket.priority,
                    status=ticket.status,
                )
            )
            await session.commit()

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

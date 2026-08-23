"""
Idempotent ticket persistence boundary for the Supervisor adapter.

``create_ticket`` itself is pure and immutable (see ticket_agent.py) — it
cannot dedupe because it has no memory. Deduplication lives here instead, in
the one place the adapter hands a created ``Ticket`` to something stateful.
An ``IdempotentTicketStore`` records every ``(idempotency_key -> Ticket)`` it
has produced; a retried create with the same key returns the previously
created ``Ticket`` instead of booking a second one, so the caller renders the
*existing* ticket's confirmation rather than a fresh row.

The in-memory implementation is the MVP default (same spirit as
``MemorySaver`` for the checkpointer). A durable backend (the ``ticket``
table in ``db/models.py:318``) swaps in at Phase 5 behind the same interface —
the adapter only ever sees the store's ``create_ticket(...)``.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import smtplib
from email.message import EmailMessage

from sqlalchemy import insert

from agents.ticket_agent.ticket_agent import call as _call
from agents.ticket_agent.ticket_agent import create_ticket as _build_ticket
from agents.ticket_agent.types import PendingTicket, Ticket


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


def _send_ticket_email(email: str, ticket_id: str, query: str) -> None:
    """Send a ticket creation email via SMTP.

    Raises ``smtplib.SMTPException`` on failure — the caller may choose
    to log or ignore the error so that a failed send does not block
    ticket creation.
    """
    config = _get_smtp_config()
    host = config.get("host")
    port = config.get("port")
    username = config.get("username")
    password = config.get("password")
    from_email = config.get("from_email")

    # If any required config is missing, do nothing (backward compatible).
    if not host or not port or not username or not password or not from_email:
        return

    subject = "Your ticket has been created (ID: {ticket_id})"

    body = f"""Dear {email},

Your ticket has been successfully created.

────────────────────────────────────────────────────────
Ticket ID: {ticket_id}
Query: {query}
Status: We'll follow up with you at {email}.

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

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = email
    msg.set_content(body)

    try:
        if username and password:
            smtp = smtplib.SMTP(host, port)
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
            smtp.quit()
        else:
            smtp = smtplib.SMTP(host, port)
            smtp.starttls()
            smtp.send_message(msg)
            smtp.quit()
    except Exception:
        # Log but do not raise — ticket creation should succeed regardless.
        pass


class TicketStore:
    """Handles idempotency for ticket creation.

    This is the full ``ticket_ops`` object the Supervisor adapter drives:
    ``call(query)`` on the opening turn and ``create_ticket(...)`` on the
    resume turn, so a ``TicketStore`` instance can be passed directly as
    ``build_supervisor_graph(ticket_ops=...)``.

    ``create_ticket(pending, email, idempotency_key)``:

    - With an ``idempotency_key`` already seen: returns the previously
      created ``Ticket`` (no second creation, no state change).
    - Otherwise: builds a fresh ``Ticket``, records it in-memory keyed by
      ``idempotency_key`` when supplied, appends to ``rows`` so tests can
      count created rows, and persistently inserts into the ``ticket`` table
      when a ``session_factory`` is configured.

    ``next_sequence(scope)`` returns the count of tickets already recorded
    under an idempotency-key scope prefix (e.g. a ``thread_id``) — the
    server-derived part of the idempotency key (§2.3).

    ``rows`` is the observable "table" the adapter's persistence step and
    the tests assert against.
    """

    def __init__(self, session_factory=None) -> None:
        self._by_key: dict[str, Ticket] = {}
        self.rows: list[Ticket] = []
        self._session_factory = session_factory

    def call(self, query: str) -> PendingTicket:
        """Open a ticket for ``query`` — compose so the store is a complete
        ``ticket_ops`` for the Supervisor adapter."""
        return _call(query)

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

        # Persist to database if session_factory is configured
        if self._session_factory is not None:
            from db.async_session import session_factory as _sf
            async with _sf() as session:
                from db.models import Ticket as _DbTicket
                await session.execute(
                    insert(_DbTicket).values(
                        ticket_id=ticket.ticket_id,
                        email=ticket.email,
                        query=ticket.query,
                        priority=ticket.priority,
                        status=ticket.status,
                    )
                )
                await session.commit()

        # Send ticket creation email if SMTP is configured
        _send_ticket_email(ticket.email, ticket.ticket_id, ticket.query)

        return ticket

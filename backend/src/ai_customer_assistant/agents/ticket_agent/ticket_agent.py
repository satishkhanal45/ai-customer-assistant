"""
Ticket agent: query -> PendingTicket -> Ticket.

Scope of this module (per project decision): produce a fully-formed,
validated Ticket object and hand it back to the caller. Persisting that
Ticket to a database/table is a separate concern owned elsewhere and is
intentionally NOT done here.

When this path is reached (per project decision): only when a customer
explicitly confirms they want human help after an in-domain query the
Knowledge Agent could not ground an answer for (the "Customer wants
human help? -> Yes" branch). Out-of-domain queries are filtered out
upstream by the Supervisor Agent's domain gate and never reach this
module at all — this module has no "is this in scope for a ticket"
logic of its own, by design, since that decision has already been made
before call() is ever invoked.

Two-step shape, mirroring the actual conversation flow:
    1. call(query)              -> PendingTicket   (ticket opened, no email yet)
    2. create_ticket(pending, email) -> Ticket      (email collected + validated)

open_ticket() is provided as a convenience that composes both steps for
callers that already have both the query and the email in hand at once.

ticket_id generation: UUID4, chosen so this module never needs a
database or counter of its own to produce a unique id — storage is out
of scope here, so this module cannot rely on an auto-incrementing
primary key that only a database could assign.
"""

from __future__ import annotations

import uuid

from agents.ticket_agent.types import PendingTicket, Ticket
from agents.ticket_agent.validation import validate_email


def call(query: str) -> PendingTicket:
    """
    Open a ticket for ``query``.

    This is the "call(query)" step: it does nothing but wrap the query
    as a PendingTicket, signaling that human follow-up has been
    requested and an email is now needed to complete the ticket.

    Args:
        query: The original customer query that needs human follow-up.

    Returns:
        A PendingTicket carrying the query, awaiting an email.
    """
    return PendingTicket(query=query)


def create_ticket(pending: PendingTicket, email: str) -> Ticket:
    """
    Attach a validated email to ``pending`` and finalize it into a Ticket.

    Args:
        pending: The PendingTicket produced by call().
        email: The customer-submitted email address.

    Returns:
        A fully-formed, immutable Ticket with priority left unset
        (None) — see Ticket's docstring for why.

    Raises:
        validation.InvalidEmailError: if ``email`` is not a
            syntactically valid email address.
    """
    normalized_email = validate_email(email)

    return Ticket(
        ticket_id=str(uuid.uuid4()),
        email=normalized_email,
        query=pending.query,
        priority=None,
    )


def open_ticket(query: str, email: str) -> Ticket:
    """
    Convenience wrapper composing call() and create_ticket() in one step,
    for callers that already have both the query and email available.

    Args:
        query: The original customer query that needs human follow-up.
        email: The customer-submitted email address.

    Returns:
        A fully-formed Ticket, identical to calling
        create_ticket(call(query), email).

    Raises:
        validation.InvalidEmailError: if ``email`` is not a
            syntactically valid email address.
    """
    return create_ticket(call(query), email)

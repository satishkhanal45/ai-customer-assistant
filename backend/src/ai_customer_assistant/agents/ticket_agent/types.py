"""
Data layer for the ticket agent.

Contains ONLY data definitions — no business logic, no I/O, no side
effects. Every structure here is immutable (frozen dataclasses) so a
Ticket, once created, cannot be silently mutated by any downstream code
(notifications, storage, etc.) that this project doesn't own.

Contract boundaries:
    PendingTicket -> produced by call(query); a ticket request that has
                     a query but no email yet.
    Ticket        -> produced by create_ticket(); a fully-formed ticket
                     ready to be handed to storage (out of scope here).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PendingTicket:
    """
    A ticket request that has been opened for a query, but does not yet
    have a validated customer email attached.

    Attributes:
        query: The original customer query that could not be answered
               and needs human follow-up.
    """

    query: str


@dataclass(frozen=True, slots=True)
class Ticket:
    """
    A fully-formed ticket, ready to be handed to a storage layer
    (out of scope for this module).

    Attributes:
        ticket_id: Globally unique identifier (UUID4 string).
        email: The customer's email, in normalized form as returned by
               the email validator, case preserved as submitted.
        query: The original customer query.
        priority: Left as None for now — deliberately unset so a
                  priority-assignment step can be added later without
                  changing this schema (see module docstring in
                  ticket_agent.py for why).
    """

    ticket_id: str
    email: str
    query: str
    priority: str | None = None

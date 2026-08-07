"""
Unit tests for ai_customer_assistant/agents/ticket_agent/.

No network calls are made anywhere in this suite: email validation is
syntax-only (see validation.py — check_deliverability=False), so these
tests run fully offline, consistent with the rest of this project's
test suite.
"""

from __future__ import annotations

import dataclasses

import pytest

from agents.ticket_agent.ticket_agent import call, create_ticket, open_ticket
from agents.ticket_agent.types import PendingTicket, Ticket
from agents.ticket_agent.validation import InvalidEmailError, validate_email


class TestCall:
    def test_returns_pending_ticket_with_query(self) -> None:
        pending = call("What does a custom mural cost?")
        assert isinstance(pending, PendingTicket)
        assert pending.query == "What does a custom mural cost?"

    def test_pending_ticket_is_immutable(self) -> None:
        pending = call("some query")
        with pytest.raises(dataclasses.FrozenInstanceError):
            pending.query = "changed"  # type: ignore[misc]


class TestValidateEmail:
    def test_accepts_valid_email(self) -> None:
        assert validate_email("jane@example.com") == "jane@example.com"

    def test_domain_is_lowercased(self) -> None:
        assert validate_email("jane@EXAMPLE.com") == "jane@example.com"

    def test_local_part_case_is_preserved(self) -> None:
        assert validate_email("Jane.Doe@example.com") == "Jane.Doe@example.com"

    def test_rejects_missing_at_sign(self) -> None:
        with pytest.raises(InvalidEmailError):
            validate_email("not-an-email")

    def test_rejects_missing_domain(self) -> None:
        with pytest.raises(InvalidEmailError):
            validate_email("jane@")

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(InvalidEmailError):
            validate_email("")

    def test_accepts_any_valid_domain_not_just_gmail(self) -> None:
        # Confirms no provider-specific restriction (per project decision).
        assert validate_email("buyer@company.org") == "buyer@company.org"


class TestCreateTicket:
    def test_creates_ticket_with_validated_email(self) -> None:
        pending = call("Do you offer bulk pricing?")
        ticket = create_ticket(pending, "buyer@company.org")
        assert isinstance(ticket, Ticket)
        assert ticket.email == "buyer@company.org"
        assert ticket.query == "Do you offer bulk pricing?"

    def test_ticket_id_is_a_valid_uuid4_string(self) -> None:
        import uuid

        pending = call("some query")
        ticket = create_ticket(pending, "buyer@company.org")
        parsed = uuid.UUID(ticket.ticket_id, version=4)
        assert str(parsed) == ticket.ticket_id

    def test_two_tickets_get_different_ids(self) -> None:
        pending = call("some query")
        first = create_ticket(pending, "buyer@company.org")
        second = create_ticket(pending, "buyer@company.org")
        assert first.ticket_id != second.ticket_id

    def test_priority_defaults_to_none(self) -> None:
        pending = call("some query")
        ticket = create_ticket(pending, "buyer@company.org")
        assert ticket.priority is None

    def test_invalid_email_raises_and_no_ticket_is_created(self) -> None:
        pending = call("some query")
        with pytest.raises(InvalidEmailError):
            create_ticket(pending, "not-an-email")

    def test_ticket_is_immutable(self) -> None:
        pending = call("some query")
        ticket = create_ticket(pending, "buyer@company.org")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ticket.priority = "high"  # type: ignore[misc]


class TestOpenTicket:
    def test_equivalent_to_call_then_create_ticket(self) -> None:
        via_open = open_ticket("some query", "buyer@company.org")
        pending = call("some query")
        via_steps = create_ticket(pending, "buyer@company.org")

        assert via_open.email == via_steps.email
        assert via_open.query == via_steps.query
        assert via_open.priority == via_steps.priority
        # ticket_id differs (each is a fresh uuid4) — identity isn't compared.

    def test_invalid_email_raises(self) -> None:
        with pytest.raises(InvalidEmailError):
            open_ticket("some query", "not-an-email")

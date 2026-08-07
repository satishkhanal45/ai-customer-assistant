"""
Unit tests for agents/safety_agent/fallback_response.py and report.py.

report.py's output shape is verified against the real Supervisor
contract (agents/supervisor/routing.py:decide_post_downstream) in a
separate integration check during development — see project
documentation. These tests verify this module's own guarantees in
isolation: no network, no real GroundednessResult computation needed.
"""

from __future__ import annotations

import pytest

from agents.safety_agent.fallback_response import generate_fallback_response
from agents.safety_agent.types import GroundednessResult
from agents.safety_agent.report import (
    STATUS_GROUNDED,
    STATUS_UNGROUNDED,
    report_grounded,
    report_ungrounded,
)


class TestGenerateFallbackResponse:
    def test_includes_the_query(self) -> None:
        message = generate_fallback_response("What is your return policy?")
        assert "What is your return policy?" in message

    def test_strips_surrounding_whitespace_from_query(self) -> None:
        message = generate_fallback_response("  What is your return policy?  ")
        assert "  What is your return policy?  " not in message
        assert "What is your return policy?" in message

    def test_offers_human_escalation(self) -> None:
        message = generate_fallback_response("anything")
        assert "support team" in message.lower() or "human" in message.lower()

    def test_is_deterministic(self) -> None:
        # Static/templated, per project decision -- not randomized or
        # dependent on any external call.
        first = generate_fallback_response("same query")
        second = generate_fallback_response("same query")
        assert first == second


class TestReportGrounded:
    def test_status_is_grounded(self) -> None:
        payload = report_grounded("The answer.")
        assert payload["status"] == STATUS_GROUNDED
        assert payload["status"] == "GROUNDED"

    def test_response_passed_through_unchanged(self) -> None:
        payload = report_grounded("Exact answer text with citations [1].")
        assert payload["response"] == "Exact answer text with citations [1]."

    def test_customer_wants_escalation_always_false(self) -> None:
        payload = report_grounded("The answer.")
        assert payload["customer_wants_escalation"] is False

    def test_matches_supervisor_expected_keys_exactly(self) -> None:
        payload = report_grounded("The answer.")
        assert set(payload.keys()) == {"status", "response", "customer_wants_escalation"}


class TestReportUngrounded:
    def _fake_result(self) -> GroundednessResult:
        return GroundednessResult(is_grounded=False, confidence_score=0.4, sentence_scores=())

    def test_status_is_ungrounded(self) -> None:
        payload = report_ungrounded("some query", self._fake_result())
        assert payload["status"] == STATUS_UNGROUNDED
        assert payload["status"] == "UNGROUNDED"

    def test_response_is_the_fallback_message(self) -> None:
        payload = report_ungrounded("What is your return policy?", self._fake_result())
        assert "What is your return policy?" in payload["response"]

    def test_customer_wants_escalation_defaults_false(self) -> None:
        payload = report_ungrounded("some query", self._fake_result())
        assert payload["customer_wants_escalation"] is False

    def test_customer_wants_escalation_can_be_set_true(self) -> None:
        payload = report_ungrounded(
            "some query", self._fake_result(), customer_wants_escalation=True
        )
        assert payload["customer_wants_escalation"] is True

    def test_matches_supervisor_expected_keys_exactly(self) -> None:
        payload = report_ungrounded("some query", self._fake_result())
        assert set(payload.keys()) == {"status", "response", "customer_wants_escalation"}

"""A degraded turn must leave evidence and say something useful (F3).

Two places catch every exception so one bad turn cannot take the process
down — the Supervisor's classification node and its Knowledge adapter. Both
used to swallow silently. The whole diagnostic for a failed turn was:

    "downstream_result": {"status": "ERROR", "error": "error"}

and the customer got *"Sorry, something went wrong on my end."*

That is not enough to act on from either side. During live UI testing the
same silent path hid a Groq 429 twice; finding the cause needed a separate
reproduction script, because nothing in the log distinguished a quota
ceiling from a genuine defect. A rate limit is also not "something went
wrong" — it is a wait, and telling the customer so gives them something to
do about it.

These tests assert both halves: the exception reaches the log, and the
customer-facing wording reflects what actually happened.
"""
from __future__ import annotations

import asyncio
import logging

from agents.supervisor.agents_wiring import _error_result, make_knowledge_agent_node
from agents.supervisor.node import make_classify_and_route_node
from agents.supervisor.routing import (
    _BUSY_RESPONSE,
    _SAFE_FALLBACK_RESPONSE,
    failure_reason,
    failure_response,
    is_rate_limited,
)


class _RateLimitError(Exception):
    """Shaped like a provider's rate-limit exception."""


def _wrapped_rate_limit() -> Exception:
    """A 429 as it actually arrives: re-raised inside the Knowledge stage's
    own exception type, so only the cause chain still carries the marker."""
    try:
        try:
            raise _RateLimitError(
                "Error code: 429 - {'error': {'message': 'Rate limit reached for "
                "model `openai/gpt-oss-120b` ... Please try again in 21m29s'}}"
            )
        except Exception as inner:
            raise RuntimeError(f"query-rewrite LLM call failed: {inner}") from inner
    except Exception as outer:
        return outer


class TestRateLimitDetection:
    def test_a_wrapped_429_is_recognised(self):
        assert is_rate_limited(_wrapped_rate_limit()) is True

    def test_a_bare_provider_exception_type_is_recognised(self):
        assert is_rate_limited(_RateLimitError("slow down")) is True

    def test_provider_naming_variants_are_recognised(self):
        """groq calls it RateLimitError; others differ. A vendor renaming its
        exception class must not silently turn rate limits back into
        "something went wrong"."""
        for name in ("RateLimitError", "RateLimitExceeded", "TooManyRequests"):
            exc = type(name, (Exception,), {})("slow down")
            assert is_rate_limited(exc) is True, name

    def test_a_status_code_attribute_is_recognised(self):
        exc = RuntimeError("upstream said no")
        exc.status_code = 429
        assert is_rate_limited(exc) is True

    def test_an_ordinary_failure_is_not_a_rate_limit(self):
        assert is_rate_limited(RuntimeError("connection reset by peer")) is False
        assert is_rate_limited(ValueError("bad JSON")) is False

    def test_no_exception_is_not_a_rate_limit(self):
        assert is_rate_limited(None) is False

    def test_a_cycle_in_the_cause_chain_terminates(self):
        """`raise ... from` can be made cyclic; walking it must not hang."""
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        assert is_rate_limited(a) is False

    def test_the_two_failures_read_differently_to_a_customer(self):
        assert failure_response(_wrapped_rate_limit()) == _BUSY_RESPONSE
        assert failure_response(RuntimeError("boom")) == _SAFE_FALLBACK_RESPONSE
        assert _BUSY_RESPONSE != _SAFE_FALLBACK_RESPONSE

    def test_the_reason_label_distinguishes_them(self):
        assert failure_reason(_wrapped_rate_limit()) == "rate_limited"
        assert failure_reason(RuntimeError("boom")) == "error"


class _ExplodingClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def classify(self, system_prompt, user_message, conversation_history):
        raise self._exc


class TestClassificationFailureIsVisible:
    def test_the_exception_reaches_the_log(self, caplog):
        node = make_classify_and_route_node(_ExplodingClient(RuntimeError("boom")))
        with caplog.at_level(logging.WARNING):
            node({"user_message": "hello", "conversation_history": []})

        assert caplog.records, "a swallowed classification failure logged nothing"
        record = caplog.records[-1]
        assert record.exc_info is not None, "the traceback must be attached"
        assert "RuntimeError" in record.getMessage()

    def test_the_turn_still_degrades_rather_than_raising(self):
        node = make_classify_and_route_node(_ExplodingClient(RuntimeError("boom")))
        result = node({"user_message": "hello", "conversation_history": []})
        assert result["final_response"] == _SAFE_FALLBACK_RESPONSE

    def test_an_in_domain_question_is_never_declined_as_out_of_scope(self):
        """The point of the transient path: a model failure must not be
        rendered as a refusal, which would look like a deliberate decision."""
        node = make_classify_and_route_node(_ExplodingClient(RuntimeError("boom")))
        result = node({"user_message": "what are your rates?", "conversation_history": []})
        assert result["next_agent"] != "TICKET_AGENT"
        assert result["clarification_required"] is False

    def test_a_rate_limit_tells_the_customer_to_retry(self, caplog):
        node = make_classify_and_route_node(_ExplodingClient(_wrapped_rate_limit()))
        with caplog.at_level(logging.WARNING):
            result = node({"user_message": "hello", "conversation_history": []})
        assert result["final_response"] == _BUSY_RESPONSE
        assert caplog.records


class _ExplodingGraph:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def ainvoke(self, payload):
        raise self._exc


class _HangingGraph:
    async def ainvoke(self, payload):
        await asyncio.sleep(10)


class TestKnowledgeFailureIsVisible:
    async def test_the_exception_reaches_the_log(self, caplog):
        node = make_knowledge_agent_node(_ExplodingGraph(RuntimeError("boom")))
        with caplog.at_level(logging.WARNING):
            result = await node({"user_message": "q", "conversation_history": []})

        assert caplog.records, "a swallowed knowledge failure logged nothing"
        assert caplog.records[-1].exc_info is not None
        assert result["downstream_result"]["status"] == "ERROR"

    async def test_a_rate_limit_is_labelled_and_worded_as_one(self, caplog):
        node = make_knowledge_agent_node(_ExplodingGraph(_wrapped_rate_limit()))
        with caplog.at_level(logging.WARNING):
            result = await node({"user_message": "q", "conversation_history": []})

        downstream = result["downstream_result"]
        assert downstream["error"] == "rate_limited", (
            "a bare 'error' cannot be told apart from a real defect"
        )
        assert downstream["response"] == _BUSY_RESPONSE

    async def test_an_ordinary_failure_keeps_the_generic_wording(self):
        node = make_knowledge_agent_node(_ExplodingGraph(RuntimeError("boom")))
        result = await node({"user_message": "q", "conversation_history": []})
        assert result["downstream_result"]["error"] == "error"
        assert result["downstream_result"]["response"] == _SAFE_FALLBACK_RESPONSE

    async def test_a_timeout_is_logged_with_the_budget_it_exceeded(self, caplog):
        """Exceeding the budget is the single most useful line to find when
        investigating latency, and it used to produce none."""
        node = make_knowledge_agent_node(_HangingGraph(), timeout_s=0.01)
        with caplog.at_level(logging.WARNING):
            result = await node({"user_message": "q", "conversation_history": []})

        assert result["downstream_result"]["error"] == "timeout"
        assert any("budget" in r.getMessage() for r in caplog.records)

    async def test_the_log_does_not_carry_the_customer_message(self, caplog):
        """P0-4 is still open — these new lines must not add to the problem."""
        node = make_knowledge_agent_node(_ExplodingGraph(RuntimeError("boom")))
        with caplog.at_level(logging.WARNING):
            await node(
                {"user_message": "my email is alice@example.com", "conversation_history": []}
            )
        assert not any("alice@example.com" in r.getMessage() for r in caplog.records)


class TestErrorResultShape:
    def test_the_default_wording_is_unchanged(self):
        assert _error_result("error")["downstream_result"]["response"] == _SAFE_FALLBACK_RESPONSE

    def test_an_override_replaces_only_the_wording(self):
        result = _error_result("rate_limited", response=_BUSY_RESPONSE)["downstream_result"]
        assert result["status"] == "ERROR"
        assert result["error"] == "rate_limited"
        assert result["response"] == _BUSY_RESPONSE

"""The request timeout ladder must never invert (P2-4).

The browser gave up on `POST /chat` after 60 seconds while the Knowledge node
was allowed 120 and a rate-limited Groq call could sleep for 300. A turn
taking 60-120s was therefore abandoned in the browser while the server
finished it and wrote the checkpoint: the customer saw "Request timed out"
and their next message resumed from a state they had never been shown.

Nothing logged an error, because each layer behaved exactly as configured.
That is what makes this worth a test rather than a comment — the failure is
invisible from inside any single layer, and only the *relationship* between
the numbers is wrong.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

import timeouts

REPO_ROOT = Path(timeouts.__file__).resolve().parents[3]
CHAT_JS = REPO_ROOT / "frontend" / "src" / "pages" / "chat.js"


class TestLadderOrdering:
    def test_every_layer_gives_up_before_the_one_waiting_on_it(self):
        assert timeouts.LLM_CALL_TIMEOUT_S <= timeouts.LLM_RETRY_BUDGET_S
        assert timeouts.LLM_RETRY_BUDGET_S < timeouts.KNOWLEDGE_NODE_TIMEOUT_S
        assert timeouts.KNOWLEDGE_NODE_TIMEOUT_S < timeouts.CLIENT_REQUEST_TIMEOUT_S

    def test_an_inverted_ladder_is_refused(self, monkeypatch):
        """A misconfiguration here produces no error at runtime, so it has to
        fail at boot or it will not be noticed at all."""
        monkeypatch.setattr(timeouts, "KNOWLEDGE_NODE_TIMEOUT_S", 999.0)
        with pytest.raises(ValueError, match="ladder inverted"):
            timeouts.assert_ladder_is_consistent()

    def test_the_shipped_defaults_are_consistent(self):
        timeouts.assert_ladder_is_consistent()  # must not raise


class TestTheGraphUsesTheLadder:
    def test_knowledge_node_default_comes_from_timeouts(self):
        import inspect

        from agents.supervisor.agents_wiring import make_knowledge_agent_node
        from agents.supervisor.graph import build_supervisor_graph

        node_default = inspect.signature(make_knowledge_agent_node).parameters["timeout_s"].default
        graph_default = (
            inspect.signature(build_supervisor_graph).parameters["knowledge_timeout_s"].default
        )
        assert node_default == timeouts.KNOWLEDGE_NODE_TIMEOUT_S
        assert graph_default == timeouts.KNOWLEDGE_NODE_TIMEOUT_S

    def test_the_node_budget_is_under_the_client_budget(self):
        """The specific inversion that shipped: 120 > 60."""
        assert timeouts.KNOWLEDGE_NODE_TIMEOUT_S < timeouts.CLIENT_REQUEST_TIMEOUT_S


class TestFrontendAgrees:
    """The browser's budget lives in JavaScript and cannot import the module,
    so the literal is checked here instead of being allowed to drift."""

    def test_chat_js_declares_the_same_client_budget(self):
        if not CHAT_JS.is_file():
            pytest.skip(f"{CHAT_JS} not present")
        match = re.search(r"CHAT_REQUEST_TIMEOUT_MS\s*=\s*(\d+)", CHAT_JS.read_text())
        assert match, "chat.js no longer declares CHAT_REQUEST_TIMEOUT_MS"
        assert float(match.group(1)) / 1000.0 == timeouts.CLIENT_REQUEST_TIMEOUT_S

    def test_chat_js_has_no_bare_timeout_literal_left(self):
        if not CHAT_JS.is_file():
            pytest.skip(f"{CHAT_JS} not present")
        assert "timeout: 60000" not in CHAT_JS.read_text()


class TestGroqRetriesRespectTheBudget:
    """The 300-second cooldown clamp is the reason the budget is wall-clock.

    Groq answers a rate limit with "please try again in 6m33s". That hint used
    to be honoured, clamped only at five minutes — far past the point where
    the caller had already abandoned the request, so the retry burned quota
    for an answer nobody would ever receive.
    """

    @staticmethod
    def _provider(retry_budget: float):
        import groq

        from agents.knowledge.providers import GroqKnowledgeProvider

        class _AlwaysRateLimited:
            def __init__(self):
                self.attempts = 0

            def create(self, **kwargs):
                self.attempts += 1
                raise groq.APIConnectionError(
                    request=None, message="Rate limit reached. Please try again in 6m33.552s."
                )

        completions = _AlwaysRateLimited()
        provider = GroqKnowledgeProvider.__new__(GroqKnowledgeProvider)
        provider.client = type("C", (), {"chat": type("D", (), {"completions": completions})()})()
        provider.model = provider.rewrite_model = "fake"
        provider.timeout = 0.01
        provider.retry_budget = retry_budget
        return provider, completions

    def test_a_long_cooldown_hint_does_not_outlive_the_budget(self):
        import groq

        provider, completions = self._provider(retry_budget=0.5)
        started = time.monotonic()
        with pytest.raises(groq.APIConnectionError):
            provider.rewrite_complete("prompt")
        elapsed = time.monotonic() - started

        # The hint asks for 393 seconds. The budget is half a second.
        assert elapsed < 5.0, f"retry loop ran for {elapsed:.1f}s despite a 0.5s budget"
        assert completions.attempts >= 1

    def test_the_budget_is_wall_clock_not_a_per_sleep_clamp(self):
        """Clamping each individual sleep still allows N clamped sleeps. Only
        a deadline bounds the whole call."""
        from agents.knowledge import providers

        deadline = time.monotonic() + 0.05
        # Asking for a 10s backoff with 0.05s left must not sleep at all.
        started = time.monotonic()
        allowed = providers._sleep_within_budget(10.0, deadline, call_timeout=0.01)
        assert allowed is False
        assert time.monotonic() - started < 0.5

    def test_a_sleep_that_fits_is_still_taken(self):
        from agents.knowledge import providers

        deadline = time.monotonic() + 5.0
        assert providers._sleep_within_budget(0.01, deadline, call_timeout=0.01) is True


class TestSupervisorClassifyHasASocketDeadline:
    def test_classify_passes_its_timeout_to_the_api_call(self):
        """`self.timeout` was stored and never used, so classification had no
        deadline at all — a stalled connection held the turn open forever."""
        from agents.supervisor.llm_client import GroqSupervisorLLMClient

        seen = {}

        class _Completions:
            def create(self, **kwargs):
                seen.update(kwargs)
                return type(
                    "R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]}
                )()

        client = GroqSupervisorLLMClient.__new__(GroqSupervisorLLMClient)
        client.client = type("C", (), {"chat": type("D", (), {"completions": _Completions()})()})()
        client.model = "fake"
        client.timeout = 7.5

        client.classify("system", "hello", [])
        assert seen.get("timeout") == 7.5

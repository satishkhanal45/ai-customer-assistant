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
        for call, budget in (
            (timeouts.LLM_SHORT_TIMEOUT_S, timeouts.LLM_SHORT_RETRY_BUDGET_S),
            (timeouts.LLM_ANSWER_TIMEOUT_S, timeouts.LLM_ANSWER_RETRY_BUDGET_S),
        ):
            assert call <= budget
            assert budget < timeouts.KNOWLEDGE_NODE_TIMEOUT_S
        assert timeouts.KNOWLEDGE_NODE_TIMEOUT_S < timeouts.CLIENT_REQUEST_TIMEOUT_S

    def test_an_inverted_ladder_is_refused(self, monkeypatch):
        """A misconfiguration here produces no error at runtime, so it has to
        fail at boot or it will not be noticed at all."""
        monkeypatch.setattr(timeouts, "KNOWLEDGE_NODE_TIMEOUT_S", 1.0)
        with pytest.raises(ValueError, match="ladder inverted"):
            timeouts.assert_ladder_is_consistent()

    def test_a_call_outliving_its_own_retry_loop_is_refused(self, monkeypatch):
        monkeypatch.setattr(timeouts, "LLM_ANSWER_TIMEOUT_S", 999.0)
        with pytest.raises(ValueError, match="must not outlive the retry loop"):
            timeouts.assert_ladder_is_consistent()

    def test_a_stage_that_could_eat_the_whole_node_budget_is_refused(self, monkeypatch):
        """One completion must not be allowed to consume the budget the node
        needs for three of them plus retrieval."""
        monkeypatch.setattr(
            timeouts, "LLM_ANSWER_RETRY_BUDGET_S", timeouts.KNOWLEDGE_NODE_TIMEOUT_S
        )
        with pytest.raises(ValueError, match="whole node budget"):
            timeouts.assert_ladder_is_consistent()

    def test_a_turn_reaching_the_client_budget_is_refused(self, monkeypatch):
        monkeypatch.setattr(timeouts, "TURN_BUDGET_S", timeouts.CLIENT_REQUEST_TIMEOUT_S)
        with pytest.raises(ValueError, match="browser stops listening"):
            timeouts.assert_ladder_is_consistent()


    def test_the_shipped_defaults_are_consistent(self):
        timeouts.assert_ladder_is_consistent()  # must not raise


class TestTheWholeTurnIsBudgeted:
    """Every rung being individually small enough is not sufficient (F1).

    The ladder used to bound the Knowledge node and nothing else. A turn is
    `classify + knowledge node + checkpoint writes`, and with classification
    able to burn 3 x 10s of socket timeout plus backoff, the total reached
    ~76s while every rung honoured its own budget — under a 60s client.
    """

    def test_the_server_side_budgets_sum_to_less_than_the_turn(self):
        total = (
            timeouts.CLASSIFY_BUDGET_S
            + timeouts.KNOWLEDGE_NODE_TIMEOUT_S
            + timeouts.CHECKPOINT_HEADROOM_S
        )
        assert total <= timeouts.TURN_BUDGET_S

    def test_the_turn_finishes_before_the_client_gives_up(self):
        assert timeouts.TURN_BUDGET_S < timeouts.CLIENT_REQUEST_TIMEOUT_S

    def test_a_sum_that_overruns_is_refused_even_when_each_rung_fits(self, monkeypatch):
        """The exact shape of F1: nothing here is individually larger than the
        turn budget, yet together they overrun it."""
        monkeypatch.setattr(timeouts, "CLASSIFY_BUDGET_S", 30.0)
        monkeypatch.setattr(timeouts, "KNOWLEDGE_NODE_TIMEOUT_S", 40.0)
        assert timeouts.CLASSIFY_BUDGET_S < timeouts.TURN_BUDGET_S
        assert timeouts.KNOWLEDGE_NODE_TIMEOUT_S < timeouts.TURN_BUDGET_S

        with pytest.raises(ValueError, match="sum to"):
            timeouts.assert_ladder_is_consistent()

    def test_the_node_budget_is_derived_from_what_is_left(self, monkeypatch):
        """Declaring it independently is how the arithmetic drifted out of
        agreement with the client in the first place."""
        import importlib

        monkeypatch.setenv("TURN_BUDGET_S", "55")
        monkeypatch.delenv("KNOWLEDGE_NODE_TIMEOUT_S", raising=False)
        reloaded = importlib.reload(timeouts)
        try:
            assert reloaded.KNOWLEDGE_NODE_TIMEOUT_S == (
                55.0 - reloaded.CLASSIFY_BUDGET_S - reloaded.CHECKPOINT_HEADROOM_S
            )
        finally:
            monkeypatch.delenv("TURN_BUDGET_S", raising=False)
            importlib.reload(timeouts)

    def test_classification_is_bounded_by_wall_clock(self):
        """It was bounded by attempt count only — 3 x 10s plus backoff — which
        made it the unbounded term in the sum."""
        import inspect

        from agents.supervisor.llm_client import GroqSupervisorLLMClient

        default = (
            inspect.signature(GroqSupervisorLLMClient.__init__)
            .parameters["retry_budget"]
            .default
        )
        assert default == timeouts.CLASSIFY_BUDGET_S
        assert timeouts.CLASSIFY_BUDGET_S >= timeouts.LLM_SHORT_TIMEOUT_S, (
            "the budget must fit at least one full attempt"
        )

class TestPerStageBudgets:
    """One timeout for every LLM call was wrong (F2).

    Measured against the live corpus: classify 1.1s, rewrite 0.5-1.9s,
    extract 1.0-1.4s, answer 1.3-13.6s. The shared 15s ceiling sat right on
    top of the answer stage's real range, so a healthy long generation was
    cut off as a failure, the retry burned another 15s, the budget was
    exhausted, and the node reported a bare `"error": "error"` for a call
    that would have succeeded.
    """

    def test_generation_gets_more_room_than_the_short_stages(self):
        assert timeouts.LLM_ANSWER_TIMEOUT_S > timeouts.LLM_SHORT_TIMEOUT_S

    def test_generation_clears_the_measured_worst_case(self):
        """13.6s was observed. A ceiling at or under that reproduces the bug."""
        assert timeouts.LLM_ANSWER_TIMEOUT_S >= 20.0

    def test_short_stages_are_not_given_generation_headroom(self):
        """Classification needs ~1s. Leaving it 30 means a genuinely stuck
        call holds the turn open far longer than it needs to."""
        assert timeouts.LLM_SHORT_TIMEOUT_S <= 12.0

    def test_answer_generation_uses_the_answer_budget(self):
        """The provider must actually thread the stage-specific values through
        — the constants are useless if every call site still reads one."""
        from agents.knowledge.providers import GroqKnowledgeProvider

        seen = []

        class _Completions:
            def create(self, **kwargs):
                seen.append(kwargs.get("timeout"))
                return type(
                    "R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]}
                )()

        provider = GroqKnowledgeProvider.__new__(GroqKnowledgeProvider)
        provider.client = type("C", (), {"chat": type("D", (), {"completions": _Completions()})()})()
        provider.model = provider.rewrite_model = "fake"

        provider.rewrite_complete("p")
        provider.extraction_complete("p")
        provider.answer_complete("system", "user")

        assert seen == [
            timeouts.LLM_SHORT_TIMEOUT_S,
            timeouts.LLM_SHORT_TIMEOUT_S,
            timeouts.LLM_ANSWER_TIMEOUT_S,
        ]

    def test_supervisor_classification_uses_the_short_budget(self):
        import inspect

        from agents.supervisor.llm_client import GroqSupervisorLLMClient

        default = inspect.signature(GroqSupervisorLLMClient.__init__).parameters["timeout"].default
        assert default == timeouts.LLM_SHORT_TIMEOUT_S


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

    def test_the_stream_heartbeat_is_well_under_the_client_idle_timeout(self):
        """Streaming adds a new rung, and it can invert like any other: if the
        server ever goes quieter than the client's patience, the browser
        aborts a perfectly healthy turn — F1 again, in a new place."""
        if not CHAT_JS.is_file():
            pytest.skip(f"{CHAT_JS} not present")
        from services.chat_service import _HEARTBEAT_SECONDS

        api_js = CHAT_JS.parent.parent / "api.js"
        match = re.search(r"idleTimeout\s*\|\|\s*(\d+)", api_js.read_text())
        assert match, "api.js no longer declares a default idleTimeout"
        idle_seconds = float(match.group(1)) / 1000.0

        assert _HEARTBEAT_SECONDS * 2 < idle_seconds, (
            f"heartbeat {_HEARTBEAT_SECONDS}s leaves no margin under a "
            f"{idle_seconds}s client idle timeout — one dropped beat would "
            f"abort a healthy stream"
        )

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
        allowed = providers.sleep_within_budget(10.0, deadline, call_timeout=0.01)
        assert allowed is False
        assert time.monotonic() - started < 0.5

    def test_a_sleep_that_fits_is_still_taken(self):
        from agents.knowledge import providers

        deadline = time.monotonic() + 5.0
        assert providers.sleep_within_budget(0.01, deadline, call_timeout=0.01) is True


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

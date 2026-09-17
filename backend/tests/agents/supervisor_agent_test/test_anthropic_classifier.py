"""The Anthropic classification backend, and provider auto-selection.

The Supervisor could classify with Groq or Gemini but not Anthropic, while
the Knowledge Agent could. A deployment holding only an Anthropic key
therefore answered with Claude but *classified* with the stub -- which
returns OUT_OF_SCOPE/UNKNOWN for everything and raises nothing, so the
failure was a system that started cleanly and declined every question.

These tests pin both halves: the client itself, and the resolution order
that reaches it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.contracts import ConversationTurn
from agents.supervisor.llm_client import (
    AnthropicSupervisorLLMClient,
    StubSupervisorLLMClient,
    _anthropic_messages,
    _extract_anthropic_text,
    build_llm_client,
)


# ---------------------------------------------------------------------------
# Message normalisation (pure)
# ---------------------------------------------------------------------------


def _turn(role: str, content: str) -> ConversationTurn:
    return ConversationTurn(role=role, content=content)


def test_history_and_message_alternate_starting_with_user():
    messages = _anthropic_messages(
        "and the price?",
        [_turn("user", "what is MVP development?"), _turn("assistant", "It is...")],
    )

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "and the price?"


def test_leading_assistant_turns_are_dropped():
    """`node._bounded_history` keeps a tail, so it can hand us a history
    that opens mid-pair. Anthropic rejects a leading assistant message."""
    messages = _anthropic_messages(
        "and the price?",
        [_turn("assistant", "orphaned answer"), _turn("user", "a question")],
    )

    assert [m["role"] for m in messages] == ["user"]
    assert "orphaned answer" not in messages[0]["content"]
    assert messages[0]["content"] == "a question\n\nand the price?"


def test_consecutive_same_role_turns_are_merged():
    messages = _anthropic_messages(
        "third",
        [_turn("user", "first"), _turn("user", "second")],
    )

    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == "first\n\nsecond\n\nthird"


def test_empty_history_yields_a_single_user_message():
    assert _anthropic_messages("hello", []) == [
        {"role": "user", "content": "hello"}
    ]


def test_extract_text_joins_text_blocks_and_ignores_others():
    content = [
        SimpleNamespace(type="text", text='"a": 1'),
        SimpleNamespace(type="thinking", thinking="ignored"),
        SimpleNamespace(type="text", text=', "b": 2'),
    ]
    assert _extract_anthropic_text(content) == '"a": 1, "b": 2'


def test_extract_text_survives_an_unexpected_shape():
    assert _extract_anthropic_text(None) == ""


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class _FakeMessages:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=reply)]
        )


def _client(replies):
    client = AnthropicSupervisorLLMClient.__new__(AnthropicSupervisorLLMClient)
    client.client = SimpleNamespace(messages=_FakeMessages(replies))
    client.model = "claude-sonnet-5"
    return client


def test_classify_prefills_a_brace_and_returns_parseable_json():
    """There is no `response_format` on the Messages API; JSON comes from
    prefilling the assistant turn, so the brace has to be prepended back."""
    client = _client(['"request_category": "DOMAIN_REQUEST"}'])

    raw = client.classify("SYSTEM", "what is MVP development?", [])

    assert json.loads(raw) == {"request_category": "DOMAIN_REQUEST"}
    sent = client.client.messages.calls[0]
    assert sent["messages"][-1] == {"role": "assistant", "content": "{"}


def test_system_prompt_is_a_field_not_a_message():
    client = _client(["}"])

    client.classify("SYSTEM PROMPT", "hi", [])

    sent = client.client.messages.calls[0]
    assert sent["system"] == "SYSTEM PROMPT"
    assert all(m["content"] != "SYSTEM PROMPT" for m in sent["messages"])


def test_classification_is_deterministic_and_deadlined():
    client = _client(["}"])

    client.classify("SYSTEM", "hi", [])

    sent = client.client.messages.calls[0]
    assert sent["temperature"] == 0
    assert sent["timeout"] == AnthropicSupervisorLLMClient.timeout
    assert sent["max_tokens"] == AnthropicSupervisorLLMClient.max_tokens


def test_a_transient_failure_is_retried():
    client = _client([RuntimeError("connection reset"), '"intent": "UNKNOWN"}'])

    raw = client.classify("SYSTEM", "hi", [])

    assert json.loads(raw) == {"intent": "UNKNOWN"}
    assert len(client.client.messages.calls) == 2


def test_the_final_error_is_raised_not_swallowed():
    """`node.classify_and_route` turns this into the customer-facing
    apology *and* a log line; swallowing it here would lose both."""
    client = _client([RuntimeError("boom")] * 3)

    with pytest.raises(RuntimeError, match="boom"):
        client.classify("SYSTEM", "hi", [])


def test_a_rate_limit_is_noted_for_the_turn():
    """A throttled turn must be able to say so rather than claiming the
    product is broken -- the same contract the Groq client honours."""
    import rate_limit_signal

    client = _client([RuntimeError("Error code: 429 - rate limit reached")] * 3)

    with rate_limit_signal.turn_scope():
        with pytest.raises(RuntimeError):
            client.classify("SYSTEM", "hi", [])
        assert rate_limit_signal.saw_rate_limit() is True


# ---------------------------------------------------------------------------
# Auto-selection
# ---------------------------------------------------------------------------


@pytest.fixture
def only_key(monkeypatch):
    """Make exactly one provider look configured."""

    def _apply(provider: str | None, default: str = "groq"):
        import llm_credentials

        monkeypatch.setattr(
            llm_credentials, "api_key_for", lambda p: "k" if p == provider else None
        )
        monkeypatch.setattr(llm_credentials, "default_provider", lambda: default)

    return _apply


def test_an_anthropic_only_deployment_classifies_with_anthropic(only_key, monkeypatch):
    """The regression this file exists for: this used to return the stub."""
    only_key("anthropic")
    monkeypatch.setattr(
        AnthropicSupervisorLLMClient, "__init__", lambda self, **kw: None
    )

    assert isinstance(build_llm_client(), AnthropicSupervisorLLMClient)


def test_groq_still_wins_when_both_are_configured(monkeypatch):
    import llm_credentials

    from agents.supervisor.llm_client import GroqSupervisorLLMClient

    monkeypatch.setattr(llm_credentials, "api_key_for", lambda p: "k")
    monkeypatch.setattr(llm_credentials, "default_provider", lambda: "groq")
    monkeypatch.setattr(GroqSupervisorLLMClient, "__init__", lambda self, **kw: None)

    assert isinstance(build_llm_client(), GroqSupervisorLLMClient)


def test_the_admin_default_outranks_groq(only_key, monkeypatch):
    import llm_credentials

    monkeypatch.setattr(llm_credentials, "api_key_for", lambda p: "k")
    monkeypatch.setattr(llm_credentials, "default_provider", lambda: "anthropic")
    monkeypatch.setattr(
        AnthropicSupervisorLLMClient, "__init__", lambda self, **kw: None
    )

    assert isinstance(build_llm_client(), AnthropicSupervisorLLMClient)


def test_no_key_anywhere_still_starts_on_the_stub(only_key):
    only_key(None)

    assert isinstance(build_llm_client(), StubSupervisorLLMClient)

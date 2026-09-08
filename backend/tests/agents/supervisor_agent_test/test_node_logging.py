"""The customer's data must not reach the logs (P0-4).

Every Supervisor node entry and exit did `print(json.dumps(state, indent=2))`
— unconditionally, in the Docker image, with no level and no guard. On a
ticket turn that put the customer's raw message, the whole conversation
history, and **their email address** on stdout. Anyone with access to the
container logs had the transcript.

These tests hold three things:

  * nothing is emitted at the default level, and nothing is even serialised;
  * when tracing *is* on, free text is replaced by a shape summary while
    every routing field survives — so a live routing problem is still
    debuggable without reproducing what anyone said;
  * `LOG_PII=true` opts back in, deliberately, and is the only way to.

The email case gets its own test. It does not arrive in a field called
`email` — it is embedded in the ticket confirmation text, which is why the
redaction covers `response` / `final_response` rather than trusting a field
name to be honest about what it holds.
"""
from __future__ import annotations

import json
import logging

import pytest

from agents.supervisor.node_logging import (
    LOG_PII_ENV_VAR,
    log_node,
    pii_logging_enabled,
    redact,
)

CUSTOMER_EMAIL = "alice@example.com"
CUSTOMER_MESSAGE = "my card was double charged, please help"

# A turn shaped like the ticket path, where the address is inside prose.
TICKET_STATE = {
    "user_message": CUSTOMER_MESSAGE,
    "conversation_history": [
        {"role": "user", "content": CUSTOMER_MESSAGE},
        {"role": "assistant", "content": "What is your email address?"},
    ],
    "request_category": "DOMAIN_REQUEST",
    "domain_confidence": 0.95,
    "intent": "CREATE_TICKET",
    "next_agent": "TICKET_AGENT",
    "downstream_result": {
        "status": "GROUNDED",
        "response": f"Your ticket has been created. We'll follow up at {CUSTOMER_EMAIL}.",
        "citations": [],
    },
    "final_response": f"Your ticket has been created. We'll follow up at {CUSTOMER_EMAIL}.",
}


@pytest.fixture(autouse=True)
def _no_pii_optin(monkeypatch):
    monkeypatch.delenv(LOG_PII_ENV_VAR, raising=False)


def _rendered(caplog) -> str:
    return " ".join(record.getMessage() for record in caplog.records)


class TestNothingLeaks:
    def test_the_email_is_not_in_the_redacted_state(self):
        assert CUSTOMER_EMAIL not in json.dumps(redact(TICKET_STATE))

    def test_the_message_is_not_in_the_redacted_state(self):
        assert CUSTOMER_MESSAGE not in json.dumps(redact(TICKET_STATE))

    def test_nested_history_content_is_not_reachable(self):
        """The history is a list of dicts; a shallow redaction would leave the
        contents one level down."""
        rendered = json.dumps(redact(TICKET_STATE))
        assert "What is your email address?" not in rendered

    def test_a_running_node_logs_none_of_it(self, caplog):
        node = log_node("ticket_agent", lambda state: {"final_response": TICKET_STATE["final_response"]})
        with caplog.at_level(logging.DEBUG):
            node(TICKET_STATE)

        rendered = _rendered(caplog)
        assert rendered, "tracing was enabled but produced nothing"
        assert CUSTOMER_EMAIL not in rendered
        assert CUSTOMER_MESSAGE not in rendered


class TestItIsStillUsefulForDebugging:
    """A redaction that hides the routing decision would just move the
    problem: the next person debugging a live turn would turn PII back on."""

    def test_routing_fields_survive(self):
        rendered = json.dumps(redact(TICKET_STATE))
        for field in ("DOMAIN_REQUEST", "CREATE_TICKET", "TICKET_AGENT", "0.95"):
            assert field in rendered, field

    def test_the_shape_of_redacted_values_is_kept(self):
        redacted = redact(TICKET_STATE)
        assert redacted["user_message"] == f"<redacted str, {len(CUSTOMER_MESSAGE)} chars>"
        assert redacted["conversation_history"] == "<redacted list, 2 items>"

    def test_the_node_name_is_logged(self, caplog):
        node = log_node("classify_and_route", lambda state: {})
        with caplog.at_level(logging.DEBUG):
            node(TICKET_STATE)
        assert "classify_and_route" in _rendered(caplog)


class TestItIsOffByDefault:
    def test_nothing_is_emitted_at_info(self, caplog):
        node = log_node("classify_and_route", lambda state: {})
        with caplog.at_level(logging.INFO):
            node(TICKET_STATE)
        assert not caplog.records

    def test_nothing_is_serialised_when_disabled(self, caplog, monkeypatch):
        """The old code paid for `json.dumps` on every node whether or not
        anyone was reading. The guard has to come first, not last."""
        import agents.supervisor.node_logging as node_logging

        calls = []
        monkeypatch.setattr(node_logging, "jsonable", lambda value: calls.append(value))

        node = log_node("classify_and_route", lambda state: {})
        with caplog.at_level(logging.INFO):
            node(TICKET_STATE)

        assert calls == [], "state was serialised even though tracing was off"

    def test_nothing_reaches_stdout(self, capsys):
        """`print()` bypassed logging entirely — no level could silence it."""
        node = log_node("classify_and_route", lambda state: {})
        node(TICKET_STATE)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestTheOptIn:
    def test_pii_logging_is_off_unless_asked_for(self, monkeypatch):
        assert pii_logging_enabled() is False
        monkeypatch.setenv(LOG_PII_ENV_VAR, "true")
        assert pii_logging_enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
    def test_the_usual_affirmatives_are_accepted(self, monkeypatch, value):
        monkeypatch.setenv(LOG_PII_ENV_VAR, value)
        assert pii_logging_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_anything_else_stays_off(self, monkeypatch, value):
        monkeypatch.setenv(LOG_PII_ENV_VAR, value)
        assert pii_logging_enabled() is False

    def test_opting_in_returns_the_full_state(self, caplog, monkeypatch):
        """The escape hatch has to actually work, or someone will delete the
        redaction instead of setting the flag."""
        monkeypatch.setenv(LOG_PII_ENV_VAR, "true")
        node = log_node("ticket_agent", lambda state: {})
        with caplog.at_level(logging.DEBUG):
            node(TICKET_STATE)
        assert CUSTOMER_EMAIL in _rendered(caplog)


class TestTheWrapperIsTransparent:
    """Tracing must not change what the graph does."""

    def test_a_sync_node_returns_its_update_unchanged(self):
        node = log_node("n", lambda state: {"next_agent": "NONE"})
        assert node(TICKET_STATE) == {"next_agent": "NONE"}

    async def test_an_async_node_returns_its_update_unchanged(self):
        async def _node(state):
            return {"next_agent": "KNOWLEDGE_AGENT"}

        node = log_node("n", _node)
        assert await node(TICKET_STATE) == {"next_agent": "KNOWLEDGE_AGENT"}

    def test_an_interrupt_propagates_untouched(self):
        """The ticket flow pauses by raising; swallowing that would break
        multi-turn collection outright."""

        class _Interrupt(Exception):
            pass

        def _pausing(state):
            raise _Interrupt("waiting for the customer")

        with pytest.raises(_Interrupt):
            log_node("ticket_agent", _pausing)(TICKET_STATE)

    def test_unserialisable_state_does_not_break_a_turn(self, caplog):
        """A trace is a debugging aid; it must never be the thing that fails
        a customer's request."""

        class _Opaque:
            def __repr__(self):
                return "<opaque>"

        node = log_node("n", lambda state: {"ok": True})
        with caplog.at_level(logging.DEBUG):
            assert node({"weird": _Opaque()}) == {"ok": True}


def test_the_graph_no_longer_prints(capsys):
    """The compiled graph is what production runs; a wrapper that is correct
    but unwired would leave the leak in place."""
    from langgraph.checkpoint.memory import MemorySaver

    from agents.supervisor.graph import build_supervisor_graph

    class _Client:
        def classify(self, system_prompt, user_message, conversation_history):
            return (
                '{"request_category":"OUT_OF_SCOPE","domain_confidence":0.9,'
                '"intent":"UNKNOWN","intent_confidence":0.0,"clarification_question":null}'
            )

    graph = build_supervisor_graph(llm_client=_Client(), checkpointer=MemorySaver())
    graph.invoke(
        {"user_message": CUSTOMER_MESSAGE, "conversation_history": []},
        config={"configurable": {"thread_id": "p04-1"}},
    )

    captured = capsys.readouterr()
    assert CUSTOMER_MESSAGE not in captured.out
    assert CUSTOMER_MESSAGE not in captured.err

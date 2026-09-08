"""The server must answer before the browser stops listening (F1).

`ChatService.handle_message_turn` is the only place that sees a whole turn,
and therefore the only place that can make that guarantee. The timeout ladder
used to bound the Knowledge node and nothing else — but a turn is

    classify + knowledge node + checkpoint writes

and each of those honoured its own budget while the total could reach ~76s
under a 60s client budget. Live UI testing caught it: the browser rendered
"Error: Request timed out" while the server ran on to completion, wrote the
checkpoint, and discarded an answer nobody would ever see — having spent the
provider quota to produce it.

The important property is not just that a slow turn returns something. It is
that the server **stops working** when it does.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from agents.supervisor.routing import _TURN_TIMEOUT_RESPONSE
from services.chat_service import ChatService


class _SlowGraph:
    """A graph that outlives any budget, and records whether it was let go."""

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = seconds
        self.started = False
        self.ran_to_completion = False
        self.cancelled = False

    async def aget_state(self, config):
        return type("S", (), {"values": {}, "next": (), "tasks": ()})()

    async def ainvoke(self, payload, config=None):
        self.started = True
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.ran_to_completion = True
        return {"final_response": "too late to matter"}

    async def aupdate_state(self, config, values):
        return None


class _FastGraph:
    def __init__(self, reply: str = "here you go") -> None:
        self.reply = reply

    async def aget_state(self, config):
        return type("S", (), {"values": {}, "next": (), "tasks": ()})()

    async def ainvoke(self, payload, config=None):
        return {"final_response": self.reply, "downstream_result": {}}

    async def aupdate_state(self, config, values):
        return None


def _service(graph) -> ChatService:
    return ChatService(graph=graph, llm_client=object())


class TestTheTurnIsBounded:
    async def test_a_slow_turn_returns_within_the_budget(self, monkeypatch):
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 0.05)
        graph = _SlowGraph(seconds=30.0)

        reply, citations = await _service(graph).handle_message_turn("t1", "hello")

        assert reply == _TURN_TIMEOUT_RESPONSE
        assert citations == []

    async def test_the_server_actually_stops_working(self, monkeypatch):
        """The point of the fix. Returning a message while the graph keeps
        running would still burn quota on an undeliverable answer — which is
        exactly what happened when only the *client* gave up."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 0.05)
        graph = _SlowGraph(seconds=30.0)

        await _service(graph).handle_message_turn("t1", "hello")
        await asyncio.sleep(0.1)  # let any surviving task run on

        assert graph.started is True
        assert graph.cancelled is True, "the graph was left running after the turn gave up"
        assert graph.ran_to_completion is False

    async def test_the_timeout_is_logged_with_the_budget_it_exceeded(
        self, monkeypatch, caplog
    ):
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 0.05)

        with caplog.at_level(logging.WARNING):
            await _service(_SlowGraph()).handle_message_turn("t1", "hello")

        assert any("budget" in r.getMessage() for r in caplog.records), (
            "a turn cut short must not be silent"
        )

    async def test_the_wording_is_not_the_generic_apology(self):
        """Running out of time is not an internal fault — the work was
        progressing. Telling the customer to retry is actionable; telling
        them something broke is not."""
        from agents.supervisor.routing import _SAFE_FALLBACK_RESPONSE

        assert _TURN_TIMEOUT_RESPONSE != _SAFE_FALLBACK_RESPONSE
        assert "again" in _TURN_TIMEOUT_RESPONSE.lower()

    async def test_the_log_does_not_carry_the_customer_message(
        self, monkeypatch, caplog
    ):
        """P0-4 is still open; this line must not add to it."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 0.05)

        with caplog.at_level(logging.WARNING):
            await _service(_SlowGraph()).handle_message_turn(
                "t1", "my card number is 4111 1111 1111 1111"
            )

        assert not any("4111" in r.getMessage() for r in caplog.records)


class TestTheHappyPathIsUntouched:
    async def test_a_fast_turn_returns_its_reply(self):
        reply, citations = await _service(_FastGraph()).handle_message_turn("t1", "hi")
        assert reply == "here you go"
        assert citations == []

    async def test_handle_message_inherits_the_budget(self, monkeypatch):
        """`handle_message` delegates, so it must be covered too — it is the
        older of the two public surfaces."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 0.05)
        reply = await _service(_SlowGraph()).handle_message("t1", "hello")
        assert reply == _TURN_TIMEOUT_RESPONSE

    async def test_a_turn_just_inside_the_budget_still_succeeds(self, monkeypatch):
        """The budget must not be so eager that it clips healthy work."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 2.0)

        class _JustInTime(_FastGraph):
            async def ainvoke(self, payload, config=None):
                await asyncio.sleep(0.05)
                return {"final_response": self.reply, "downstream_result": {}}

        reply, _ = await _service(_JustInTime()).handle_message_turn("t1", "hi")
        assert reply == "here you go"

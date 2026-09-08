"""A turn must show progress while it runs (F7).

A median turn takes 35 seconds, and the buffered `/chat` endpoint gives the
customer an animated ellipsis for all of it — no way to tell a working system
from a hung one, and no signal at all before the answer lands or the browser
gives up.

`stream_message_turn` runs the same turn through `astream` instead of
`ainvoke`, reporting each Supervisor node as it finishes. The events are
coarse on purpose: the Knowledge subgraph is invoked *inside* a node rather
than composed as a LangGraph subgraph, so its internals are not visible to
`astream`. Three honest transitions still beat a blank spinner.

The properties worth holding onto:

  * exactly one terminal event, always — a client can rely on the stream
    ending with an answer of some kind rather than simply stopping;
  * heartbeats during silence, so a client can wait on *inactivity* rather
    than on total elapsed time;
  * the same reply the buffered endpoint would have produced, because both
    paths share `_prepare_turn` / `_finalize_turn`;
  * the graph is cancelled when the client hangs up (the F1 lesson: the
    server must stop working when nobody is listening).
"""
from __future__ import annotations

import asyncio

import pytest

from agents.supervisor.routing import _TURN_TIMEOUT_RESPONSE
from services.chat_service import ChatService


class _StreamingGraph:
    """Stands in for the compiled Supervisor graph.

    Yields the `(mode, chunk)` pairs `stream_mode=["updates", "values"]`
    produces — a shape verified against the real graph before this was
    written, including that the final `values` chunk carries `__interrupt__`
    exactly as `ainvoke`'s return does.
    """

    def __init__(self, *, final=None, nodes=("classify_and_route", "knowledge_agent",
                                             "assemble_response"), delay=0.0,
                 updates=None):
        self.final = final if final is not None else {"final_response": "here you go"}
        self.nodes = nodes
        # What each node returns. The real graph puts `next_agent` in
        # classification's update, which is where the branch is read from.
        self.updates = updates or {}
        self.delay = delay
        self.cancelled = False
        self.updated_state = None

    async def aget_state(self, config):
        return type("S", (), {"values": {}, "next": (), "tasks": ()})()

    async def astream(self, payload, config=None, stream_mode=None):
        try:
            yield "values", {"user_message": "q"}
            for node in self.nodes:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield "updates", {node: self.updates.get(node, {})}
                yield "values", dict(self.final)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aupdate_state(self, config, values):
        self.updated_state = values


def _service(graph) -> ChatService:
    return ChatService(graph=graph, llm_client=object())


async def _collect(service, **kwargs) -> list[dict]:
    return [event async for event in service.stream_message_turn("t1", "hello", **kwargs)]


class TestProgressReachesTheClient:
    async def test_stages_arrive_before_the_answer(self):
        events = await _collect(_service(_StreamingGraph()))

        kinds = [e["type"] for e in events]
        assert kinds[-1] == "result"
        assert "stage" in kinds
        assert kinds.index("stage") < kinds.index("result")

    async def test_the_first_stage_arrives_before_any_node_finishes(self):
        """The customer needs something *immediately*; waiting for the first
        node to complete would still leave several seconds of blank spinner."""
        events = await _collect(_service(_StreamingGraph()))
        assert events[0] == {"type": "stage", "stage": "understanding",
                             "label": "Understanding your question"}

    async def test_the_stages_describe_what_happens_next(self):
        """`astream` reports a node when it *finishes*, so labelling an event
        with that node's own work would always be one step behind."""
        events = await _collect(_service(_StreamingGraph()))
        labels = [e["label"] for e in events if e["type"] == "stage"]
        assert labels == [
            "Understanding your question",
            "Searching the knowledge base",
            "Writing your answer",
        ]

    async def test_a_ticket_turn_is_not_announced_as_a_search(self):
        """Telling someone we are searching the knowledge base while opening
        them a ticket would be worse than saying nothing."""
        graph = _StreamingGraph(
            final={"final_response": "ticket opened"},
            nodes=("classify_and_route", "ticket_agent", "assemble_response"),
            updates={"classify_and_route": {"next_agent": "TICKET_AGENT"}},
        )
        events = await _collect(_service(graph))
        labels = [e["label"] for e in events if e["type"] == "stage"]
        assert "Setting up your ticket" in labels
        assert "Searching the knowledge base" not in labels

    async def test_the_reply_matches_what_the_buffered_path_would_give(self):
        events = await _collect(_service(_StreamingGraph()))
        result = events[-1]
        assert result["type"] == "result"
        assert result["reply"] == "here you go"
        assert result["citations"] == []

    async def test_history_is_still_written(self):
        """Streaming must not skip the checkpoint update the buffered path
        does, or the conversation silently stops accumulating."""
        graph = _StreamingGraph()
        await _collect(_service(graph))
        assert graph.updated_state is not None
        assert "conversation_history" in graph.updated_state


class TestHeartbeats:
    async def test_silence_produces_heartbeats(self, monkeypatch):
        """The long gap is while the answer is generated. Without something on
        the wire a client cannot tell a slow turn from a dead connection."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "_HEARTBEAT_SECONDS", 0.02)
        events = await _collect(_service(_StreamingGraph(delay=0.12)))

        beats = [e for e in events if e["type"] == "heartbeat"]
        assert beats, "a slow turn sent nothing during the silence"
        assert all("elapsed" in b for b in beats)

    async def test_a_fast_turn_does_not_emit_noise(self, monkeypatch):
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "_HEARTBEAT_SECONDS", 30.0)
        events = await _collect(_service(_StreamingGraph()))
        assert not [e for e in events if e["type"] == "heartbeat"]


class TestItAlwaysTerminates:
    async def test_exactly_one_terminal_event(self):
        events = await _collect(_service(_StreamingGraph()))
        terminal = [e for e in events if e["type"] in {"result", "error"}]
        assert len(terminal) == 1

    async def test_a_failing_graph_yields_an_error_event_not_an_exception(self):
        class _Exploding(_StreamingGraph):
            async def astream(self, payload, config=None, stream_mode=None):
                raise RuntimeError("boom")
                yield  # pragma: no cover - makes this an async generator

        events = await _collect(_service(_Exploding()))
        assert events[-1]["type"] == "error"
        assert events[-1]["reason"] == "error"
        assert events[-1]["reply"]

    async def test_a_rate_limit_is_worded_as_one(self):
        class _RateLimitError(Exception):
            pass

        class _Limited(_StreamingGraph):
            async def astream(self, payload, config=None, stream_mode=None):
                raise _RateLimitError("Error code: 429 - Rate limit reached")
                yield  # pragma: no cover

        events = await _collect(_service(_Limited()))
        assert events[-1]["reason"] == "rate_limited"
        assert "again" in events[-1]["reply"].lower()

    async def test_the_turn_budget_still_applies(self, monkeypatch):
        """Streaming must not quietly opt out of the F1 guarantee."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "TURN_BUDGET_S", 0.05)
        monkeypatch.setattr(chat_service, "_HEARTBEAT_SECONDS", 0.01)

        events = await _collect(_service(_StreamingGraph(delay=5.0)))
        assert events[-1]["type"] == "error"
        assert events[-1]["reason"] == "timeout"
        assert events[-1]["reply"] == _TURN_TIMEOUT_RESPONSE

    async def test_hanging_up_stops_the_graph(self, monkeypatch):
        """The F1 lesson, restated for streaming: when nobody is listening the
        server must stop working rather than finish an undeliverable answer."""
        import services.chat_service as chat_service

        monkeypatch.setattr(chat_service, "_HEARTBEAT_SECONDS", 0.01)
        graph = _StreamingGraph(delay=5.0)

        stream = _service(graph).stream_message_turn("t1", "hello")
        await stream.__anext__()          # the customer sees the first stage...
        await stream.aclose()             # ...and closes the tab
        await asyncio.sleep(0.05)

        assert graph.cancelled is True, "the graph ran on after the client left"


class TestTheInterruptFlowSurvives:
    async def test_an_interrupt_is_rendered_as_the_question(self):
        """The ticket flow pauses mid-turn. The final `values` chunk carries
        `__interrupt__` just as `ainvoke`'s return does — verified against the
        real graph — so the shared finalizer handles both paths identically."""
        interrupt = type("I", (), {"value": {"type": "email-collection"}})()
        graph = _StreamingGraph(
            final={"final_response": None, "__interrupt__": (interrupt,)},
            nodes=("classify_and_route", "ticket_agent"),
        )
        events = await _collect(_service(graph))

        assert events[-1]["type"] == "result"
        assert "email address" in events[-1]["reply"]
        assert graph.updated_state is None, (
            "writing a checkpoint after an interrupt can clear it, breaking the resume"
        )

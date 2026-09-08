"""Chat API routes (Phase 5, §4.5).

Exposes the chat endpoints over the ``ChatService`` instance owned by the
FastAPI app (``app.state.chat_service``, set up in ``main.py``'s lifespan
hook).

``trace_id`` is generated per request at this boundary (§2.1 / §4.6): a new
uuid for every incoming message, propagated to the service so the whole
graph run shares one correlation id. No history is accepted from the caller.

Two surfaces, same turn:

  POST /chat          buffered — one JSON response when the turn finishes
  POST /chat/stream   server-sent events — progress, then the same answer

`/chat/stream` exists because a turn takes a median of 35 seconds, and a
buffered response means the customer stares at an animated ellipsis for all
of it with no way to tell a working system from a hung one (F7). The
buffered endpoint is kept, unchanged: it is the fallback when streaming is
unavailable, and it is what every existing test and script drives.
"""
from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    service = request.app.state.chat_service
    trace_id = str(uuid.uuid4())
    reply, citations = await service.handle_message_turn(
        payload.thread_id,
        payload.message,
        trace_id=trace_id,
    )
    return ChatResponse(
        thread_id=payload.thread_id,
        reply=reply,
        trace_id=trace_id,
        citations=citations,
    )


def _sse(event: dict) -> str:
    """Render one event in the server-sent-events wire format.

    `ensure_ascii=True` is deliberate: the payload carries answer text, and a
    stray newline inside a `data:` line would silently split one event into
    two. Escaping everything non-ASCII keeps the JSON on a single line
    whatever the answer contains.
    """
    return f"data: {json.dumps(event, ensure_ascii=True)}\n\n"


@router.post("/chat/stream")
async def chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
    """Stream one turn as server-sent events.

    The `trace_id` is sent up front, in its own event, so a customer can
    quote it while the answer is still being written rather than only after
    it arrives.

    `X-Accel-Buffering: no` matters in front of nginx, which otherwise
    buffers the whole response and hands it over at the end — turning a
    stream back into exactly the blank wait this endpoint exists to remove.
    """
    service = request.app.state.chat_service
    trace_id = str(uuid.uuid4())

    async def events() -> AsyncIterator[str]:
        yield _sse({"type": "accepted", "thread_id": payload.thread_id, "trace_id": trace_id})
        async for event in service.stream_message_turn(
            payload.thread_id, payload.message, trace_id=trace_id
        ):
            yield _sse({**event, "thread_id": payload.thread_id, "trace_id": trace_id})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

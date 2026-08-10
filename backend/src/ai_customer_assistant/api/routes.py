"""Chat API routes (Phase 5, §4.5).

Exposes the single chat endpoint over the ``ChatService`` instance owned by
the FastAPI app (``app.state.chat_service``, set up in ``main.py``'s
lifespan hook).

``trace_id`` is generated per request at this boundary (§2.1 / §4.6): a new
uuid for every incoming message, propagated to the service so the whole
graph run shares one correlation id. No history is accepted from the caller.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request

from schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    service = request.app.state.chat_service
    trace_id = str(uuid.uuid4())
    reply = await service.handle_message(
        payload.thread_id,
        payload.message,
        trace_id=trace_id,
    )
    return ChatResponse(
        thread_id=payload.thread_id,
        reply=reply,
        trace_id=trace_id,
    )
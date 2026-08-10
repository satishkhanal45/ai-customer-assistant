"""Chat API request/response contracts (Phase 5, §4.5).

The API never accepts a transcript: the caller sends only the new
``message`` plus the ``thread_id``. Conversation history lives behind the
checkpointer keyed by ``thread_id`` (see services/chat_service.py).
"""
from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    thread_id: str
    message: str


class ChatResponse(BaseModel):
    thread_id: str
    reply: str
    trace_id: str
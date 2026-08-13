"""Chat API request/response contracts (Phase 5, §4.5).

The API never accepts a transcript: the caller sends only the new
``message`` plus the ``thread_id``. Conversation history lives behind the
checkpointer keyed by ``thread_id`` (see services/chat_service.py).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    thread_id: str
    message: str


class Citation(BaseModel):
    """One source the assistant's reply is grounded on."""

    source_name: str
    page: Optional[int] = None
    version_number: Optional[int] = None
    category_name: Optional[str] = None


class ChatResponse(BaseModel):
    thread_id: str
    reply: str
    trace_id: str
    citations: list[Citation] = Field(default_factory=list)
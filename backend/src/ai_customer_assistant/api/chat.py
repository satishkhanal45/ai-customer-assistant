"""HTTP layer for the chat (RAG) feature."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.async_session import get_session
from services.chat_service import answer

router = APIRouter(prefix="/chat", tags=["chat"])


class HistoryTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[HistoryTurn] = []


class ChatSource(BaseModel):
    chunk_id: str
    page: int | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]
    matches: int


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    session: AsyncSession = Depends(get_session),
) -> ChatResponse:
    result = await answer(
        session,
        req.message,
        history=[t.model_dump() for t in req.history],
    )
    return ChatResponse(
        answer=result["answer"],
        sources=[ChatSource(**s) for s in result["sources"]],
        matches=result["matches"],
    )
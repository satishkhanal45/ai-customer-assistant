"""FastAPI app bootstrap (Phase 5, §4.5).

Owns the server-side singletons' lifecycle:

- the durable checkpointer (Postgres via ``AsyncPostgresSaver`` when the
  ``POSTGRES_*`` env block is configured, else MemorySaver) — built once at
  startup, closed at shutdown;
- the shared BGE embedding singleton (§4.6): ``build_shared_embeddings``
  constructs exactly one ``SentenceTransformer`` for the process, so the
  real Knowledge graph is *actually* built — ``build_chat_service`` only
  compiles the full RAG subgraph when both ``shared_embeddings`` and an
  async ``session_factory`` are supplied. Without this the Supervisor's
  Knowledge node stays on its Phase-0 placeholder;
- the ``ChatService``, the single dependency-construction point.

Run with: ``uvicorn main:app`` from ``src/ai_customer_assistant``.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import router
from db.checkpointer import build_checkpointer
from db.session import get_async_session_factory
from services.chat_service import build_chat_service
from services.embeddings import build_shared_embeddings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    checkpointer = await build_checkpointer()
    # Phase 6 wiring: the real Knowledge graph is compiled only when BOTH the
    # shared BGE instance and the async session factory are passed in. Lack of
    # either is a silent-failure hazard (the Supervisor silently keeps its
    # placeholder node), so never swallow the embedding-model load/DB errors.
    shared_embeddings = build_shared_embeddings()
    session_factory = get_async_session_factory()
    service = await build_chat_service(
        checkpointer=checkpointer,
        shared_embeddings=shared_embeddings,
        session_factory=session_factory,
    )
    app.state.chat_service = service
    yield
    conn = getattr(checkpointer, "conn", None)
    close = getattr(conn, "aclose", None)
    if close is not None:
        await close()


app = FastAPI(title="AI Customer Assistant", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
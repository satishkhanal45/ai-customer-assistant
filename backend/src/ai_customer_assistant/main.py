"""FastAPI app bootstrap (Phase 5, §4.5)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.graph import router as graph_router
from api.ingest import router as ingest_router
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
    # shared BGE instance and the async session factory are passed in.
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

# Chat (registered first — owns POST /chat)
app.include_router(router)
# Read-only knowledge-graph browsing (powers frontend/graph_viewer*.html)
app.include_router(graph_router)
# Document ingestion: multipart upload + URL crawl
app.include_router(ingest_router)
# NOTE: api.chat is NOT registered — its POST /chat collides with router's.
# NOTE: ingestion.storage.api (/admin/knowledge-sources) is NOT registered yet —
#       its get_storage_config / get_db_session / get_current_admin_user_id
#       dependencies still raise NotImplementedError (storage/api.py:62-83).


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


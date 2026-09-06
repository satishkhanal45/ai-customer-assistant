"""FastAPI app bootstrap (Phase 5, §4.5)."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Load backend/.env before importing anything that might read os.environ.
# This module is the ASGI entry point, so this is the right place for it --
# and previously the app got its .env only as an accidental side effect of
# importing agents.ticket_agent.store (see config.load_env for why that was a
# defect). In Docker this is a no-op: .env is excluded from the image and the
# environment comes from compose.
from config import load_env

load_env()

from api.graph import router as graph_router  # noqa: E402
from api.ingest import router as ingest_router  # noqa: E402
from api.routes import router  # noqa: E402
from db.checkpointer import build_checkpointer  # noqa: E402
from db.engine import dispose_engine, get_session_factory  # noqa: E402
from services.chat_service import build_chat_service  # noqa: E402
from services.embeddings import build_shared_embeddings  # noqa: E402

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    checkpointer = await build_checkpointer()
    # Phase 6 wiring: the real Knowledge graph is compiled only when BOTH the
    # shared BGE instance and the async session factory are passed in.
    shared_embeddings = build_shared_embeddings()
    session_factory = get_session_factory()
    service = await build_chat_service(
        checkpointer=checkpointer,
        shared_embeddings=shared_embeddings,
        session_factory=session_factory,
    )
    app.state.chat_service = service
    yield
    # Shutdown: release the checkpointer's psycopg pool and the shared
    # SQLAlchemy engine's pool, so a reload or redeploy does not leave
    # connections open server-side.
    conn = getattr(checkpointer, "conn", None)
    close = getattr(conn, "aclose", None)
    if close is not None:
        await close()
    await dispose_engine()


app = FastAPI(title="AI Customer Assistant", lifespan=lifespan)

# Cross-origin access for the frontend (opened from file:// or a dev static
# server). Restrict allow_origins in production as needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class _NoCacheMiddleware(BaseHTTPMiddleware):
    """Dev-friendly: force the browser to revalidate static assets every
    request so frontend edits show up without a manual cache purge."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not response.headers.get("cache-control"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(_NoCacheMiddleware)

# Chat (registered first — owns POST /chat)
app.include_router(router)
# Read-only knowledge-graph browsing (powers frontend/graph_viewer*.html)
app.include_router(graph_router)
# Document ingestion: multipart upload + URL crawl
app.include_router(ingest_router)
# NOTE: ingestion.storage.api (/admin/knowledge-sources) is NOT registered yet —
#       its get_storage_config / get_db_session / get_current_admin_user_id
#       dependencies still raise NotImplementedError (storage/api.py:62-83).


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Serve the frontend at the site root so the app runs same-origin:
# http://<host>:<port>/#/chat (frontend/src/config.js uses location.origin).
# Registered last so the API routers above take precedence. FRONTEND_DIR is
# /app/frontend inside the docker image; defaults to the repo's frontend dir.
_FRONTEND_DIR = os.environ.get("FRONTEND_DIR") or str(
    Path(__file__).resolve().parents[3] / "frontend"
)
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")


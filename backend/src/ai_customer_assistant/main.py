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

from api.admin import router as admin_router  # noqa: E402
from api.graph import router as graph_router  # noqa: E402
from api.ingest import router as ingest_router  # noqa: E402
from api.routes import router  # noqa: E402
from auth.router import router as auth_router  # noqa: E402
from auth.tokens import auth_secret  # noqa: E402
from db.checkpointer import build_checkpointer  # noqa: E402
from db.engine import dispose_engine, get_session_factory  # noqa: E402
from logging_config import configure_logging  # noqa: E402
from services.chat_service import build_chat_service  # noqa: E402
from services.embeddings import build_shared_embeddings  # noqa: E402

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Fail at boot, not at the first login attempt. A process serving
    # protected routes without a signing secret is not degraded, it is
    # unauthenticated -- and the hour someone discovers that should not be
    # whenever the first person happens to try to sign in. Same discipline as
    # timeouts.assert_ladder_is_consistent().
    auth_secret()
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

# Cross-origin access.
#
# The default is *none*, and that is not a restriction on anything the app
# does: `StaticFiles` below serves the frontend from this very origin, so
# every request the browser app makes is same-origin and never consults CORS
# at all. The previous `allow_origins=["*"]` was therefore enabling nothing
# and exposing everything -- it invited any page on the internet to call this
# API with the visitor's session.
#
# `CORS_ALLOW_ORIGINS` is a comma-separated allowlist for the case that
# genuinely needs it: a frontend served from somewhere else. `allow_credentials`
# is on because the session is a cookie, and the CORS specification forbids
# combining that with a wildcard -- so an explicit list is not merely
# preferable here, it is the only thing that works.
_CORS_ORIGINS = tuple(
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
)
if _CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_CORS_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
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

# Authentication: login, refresh, logout, me, and admin account creation.
# Registered first because everything below depends on it.
app.include_router(auth_router)
# Chat (owns POST /chat) — requires an authenticated member
app.include_router(router)
# Read-only knowledge-graph browsing (powers frontend/graph_viewer*.html)
app.include_router(graph_router)
# Document ingestion: multipart upload + URL crawl
app.include_router(ingest_router)
# Admin read surface: sources, jobs, stats, tickets. Admin-only.
app.include_router(admin_router)
# NOTE: ingestion.storage.api is still NOT registered, and that is now a
#       decision rather than a gap. It is a *write* path — a second
#       `POST /admin/knowledge-sources` upload — duplicating
#       `POST /ingest/upload`, which is what the Ingest page calls and what
#       the worker pipeline is built around. Its three placeholder
#       dependencies still raise NotImplementedError. The read endpoints
#       the Admin page needed live in api/admin.py instead.


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


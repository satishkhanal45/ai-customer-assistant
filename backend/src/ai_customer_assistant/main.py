import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # backend/.env

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.graph import router as graph_router
from api.chat import router as chat_router
from api.ingest import router as ingest_router

app = FastAPI(title="AI Customer Assistant")
app.include_router(graph_router)
app.include_router(chat_router)
app.include_router(ingest_router)

# Local-dev only: the Phase 3 viewer (frontend/graph_viewer.html) is often
# opened straight from disk (file://, so a null Origin). Allow all origins so
# it can reach /graph/* without configuring a reverse proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# The viewer HTML is edited in place (served straight from disk), so make sure
# browsers always revalidate it instead of reusing a stale cached copy.
@app.middleware("http")
async def no_cache_html(request, call_next):
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Serve the graph viewer so it can be opened at a real URL instead of file://.
# Locally the frontend lives at <repo>/frontend; in docker it is mounted at
# /app/frontend (see docker-compose.yml) and FRONTEND_DIR points at it.
_static_env = os.getenv("FRONTEND_DIR")
_static_candidates = [Path(_static_env)] if _static_env else []
_static_candidates += [
    Path(__file__).resolve().parents[3] / "frontend",
    Path(__file__).resolve().parents[2] / "frontend",
]
FRONTEND_DIR = next((p for p in _static_candidates if p.is_dir()), None)
if FRONTEND_DIR is not None:
    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/graph_viewer_3d.html")

    @app.get("/graph", include_in_schema=False)
    async def graph_3d() -> RedirectResponse:
        return RedirectResponse(url="/graph_viewer_3d.html")


    _app_dist = FRONTEND_DIR / "app" / "dist"
    if _app_dist.is_dir():
        @app.get("/app", include_in_schema=False)
        async def app_index() -> RedirectResponse:
            return RedirectResponse(url="/app/")

        app.mount("/app", StaticFiles(directory=_app_dist, html=True), name="react-app")

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # backend/.env

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.graph import router as graph_router

app = FastAPI(title="AI Customer Assistant")
app.include_router(graph_router)

# Local-dev only: the Phase 3 viewer (frontend/graph_viewer.html) is often
# opened straight from disk (file://, so a null Origin). Allow all origins so
# it can reach /graph/* without configuring a reverse proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
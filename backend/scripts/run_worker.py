#!/usr/bin/env python
"""
Entry point for the PGQueue ingestion worker.

    uv run --project backend python scripts/run_worker.py

UPDATED: async engine/session, matching storage/repository.py's AsyncSession
usage. Builds the async URL from db.session.database_url() (not modified --
only read) by swapping the driver scheme; confirm the real async driver
(psycopg[async] vs asyncpg) once the sync-vs-async question is settled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import warnings
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent / "src" / "ai_customer_assistant"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.session import database_url  # read-only import, not modified
from ingestion.queue.config import PGQueueSettings
from ingestion.queue.worker import WorkerDeps, run_worker


def _load_dotenv_if_available() -> None:
    """Running this script directly means backend/.env is never sourced
    automatically -- see the matching function in crawl_and_ingest.py."""
    import os
    from pathlib import Path

    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except ImportError:
        pass
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _async_database_url() -> str:
    sync_url = database_url()
    # TODO: confirm the real async driver in use (psycopg async vs asyncpg)
    return sync_url.replace("postgresql+psycopg://", "postgresql+psycopg_async://")



def _silence_noisy_loggers() -> None:
    """Suppress verbose HTTP and ML framework logging."""
    noisy_loggers = [
        "httpx",
        "httpcore",
        "urllib3",
        "sentence_transformers",
        "huggingface_hub",
        "transformers",
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    warnings.filterwarnings("ignore", category=UserWarning)

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _silence_noisy_loggers()  # ADDED
    _load_dotenv_if_available()

    engine = create_async_engine(_async_database_url())
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    deps = WorkerDeps(session_factory=session_factory, settings=PGQueueSettings())
    logging.getLogger(__name__).info(
        "ingestion worker starting, polling every %.1fs", deps.settings.poll_interval_seconds
    )
    await run_worker(deps)




if __name__ == "__main__":
    asyncio.run(main())

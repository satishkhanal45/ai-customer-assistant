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

from config import load_env
from logging_config import install_trace_filter
from db.session import database_url  # read-only import, not modified
from ingestion.pipeline import get_pipeline_resources, reset_pipeline_resources
from ingestion.queue.config import PGQueueSettings
from ingestion.queue.worker import WorkerDeps, run_worker


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
    # `[trace]` labels every line with the run that produced it. Without it,
    # concurrent lanes interleave into an unreadable stream and a job's four
    # retry attempts are indistinguishable from one another.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(trace_id)s] %(message)s",
    )
    install_trace_filter()  # must precede the first line logged through it
    _silence_noisy_loggers()  # ADDED
    load_env()

    engine = create_async_engine(_async_database_url())
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    deps = WorkerDeps(session_factory=session_factory, settings=PGQueueSettings())
    log = logging.getLogger(__name__)

    # Load the embedding model and tokenizer here, once, rather than letting
    # the first job pay for it -- and, before this, every job after it too.
    # Doing it up front also means a broken model or missing MinIO config
    # fails at startup instead of turning the first document into a mystery
    # failure.
    get_pipeline_resources()

    log.info(
        "ingestion worker starting: %d lane(s), polling every %.1fs",
        deps.settings.concurrency,
        deps.settings.poll_interval_seconds,
    )
    try:
        await run_worker(deps)
    finally:
        # The HTTP client is owned by the process now, so the process closes
        # it. Previously one was created per job and never closed at all.
        reset_pipeline_resources()
        await engine.dispose()




if __name__ == "__main__":
    asyncio.run(main())

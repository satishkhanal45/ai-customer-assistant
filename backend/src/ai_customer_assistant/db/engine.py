"""The single async engine and session factory for the whole process.

Before this module there were four independent connection sources in one
process: two `lru_cache`d engines in `db/session.py`, a module-level engine
in `db/async_session.py`, and another created at *import time* in
`api/graph.py` — plus the checkpointer's own `AsyncConnectionPool`. Each
carried its own pool with default sizing, so the real connection ceiling was
whatever they happened to add up to, and pool exhaustion under load surfaced
as an unexplained hang in whichever module happened to ask last.

Both `api/graph.py` and `db/async_session.py` carried comments asking for
this consolidation. This is it: one engine, one factory, explicit pool
settings, everything else delegates here.

Pool sizing is env-tunable because the right numbers depend on deployment
shape (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`,
`DB_POOL_RECYCLE`). The defaults are deliberately modest: the API process
also runs the Knowledge graph's hybrid fan-out, which opens two sessions
concurrently per chat turn.

The LangGraph checkpointer keeps its own psycopg pool — it is a different
driver stack (raw psycopg, not SQLAlchemy) and cannot share this one.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db.session import database_url


def async_database_url() -> str:
    """The `postgresql+psycopg` URL rewritten onto psycopg3's async dialect."""
    return database_url().replace("postgresql+psycopg://", "postgresql+psycopg_async://")


def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unparseable
    rather than crashing the process at import time over a typo'd value."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """The process-wide async engine. Cached, so every caller shares one pool."""
    return create_async_engine(
        async_database_url(),
        pool_size=_int_env("DB_POOL_SIZE", 10),
        max_overflow=_int_env("DB_MAX_OVERFLOW", 5),
        pool_timeout=_int_env("DB_POOL_TIMEOUT", 30),
        # Recycle below the typical 5-minute idle cutoff of proxies/managed
        # Postgres so a checked-out connection is never already dead.
        pool_recycle=_int_env("DB_POOL_RECYCLE", 1800),
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory.

    A factory rather than a shared session: the Knowledge graph's hybrid
    strategy runs `structured_lookup` and `vector_search` concurrently, and a
    single `AsyncSession` is not safe for concurrent use by two coroutines.
    """
    return async_sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped `AsyncSession`.

    Commits on success so work done during the request (chunks, entities,
    version status) is not silently rolled back at request end; rolls back
    and re-raises on failure.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Called from the app's lifespan shutdown
    so a reload or redeploy does not leave connections lingering server-side."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()

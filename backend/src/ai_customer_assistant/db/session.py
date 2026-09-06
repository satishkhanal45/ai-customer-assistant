import os
from contextlib import contextmanager, asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    return (
        f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@{os.environ.get('POSTGRES_HOST', 'postgres')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/{os.environ['POSTGRES_DB']}"
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(database_url(), pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@contextmanager
def get_session() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
    finally:
        session.close()


# Async access now lives in db/engine.py, which owns the one engine for the
# process. These are kept as delegating aliases so existing imports keep
# working; they no longer build an engine of their own. See db/engine.py for
# why consolidating mattered.
def get_async_engine() -> AsyncEngine:
    """Deprecated alias for ``db.engine.get_engine``."""
    from db.engine import get_engine

    return get_engine()


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Deprecated alias for ``db.engine.get_session_factory``."""
    from db.engine import get_session_factory

    return get_session_factory()


@asynccontextmanager
async def get_async_session() -> AsyncIterator[AsyncSession]:
    async with get_async_session_factory()() as session:
        yield session

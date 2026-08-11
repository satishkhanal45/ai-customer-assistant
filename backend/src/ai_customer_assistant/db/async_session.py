"""Shared async SQLAlchemy engine/session for read paths (chat, ingest).

Mirrors the one-off pattern used in ``api/graph.py`` so every API module
doesn't build its own engine. Graph/graph.py keeps its own instance to
avoid disturbing the working viewer path.
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.session import database_url


def async_database_url() -> str:
    return database_url().replace("postgresql+psycopg://", "postgresql+psycopg_async://")


_engine = create_async_engine(async_database_url())
session_factory = async_sessionmaker(bind=_engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped AsyncSession."""
    async with session_factory() as session:
        yield session
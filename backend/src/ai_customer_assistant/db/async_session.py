"""Backwards-compatible shim over ``db.engine``.

This module used to construct its own engine and session factory at import
time. It no longer does: ``db/engine.py`` owns the single engine for the
process (see its docstring for why four independent pools was a problem).

Everything here is a thin re-export so existing imports keep working:

    from db.async_session import get_session, session_factory

Prefer importing from ``db.engine`` directly in new code.
"""

from __future__ import annotations


from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import async_database_url, get_session, get_session_factory


class _LazySessionFactory:
    """Callable that resolves the shared factory on first use.

    ``session_factory`` was previously a module-level ``async_sessionmaker``
    built at import time, and callers use it as ``async with
    session_factory() as session``. Keeping that exact call shape while
    deferring construction means importing this module no longer opens a
    connection pool as a side effect — which matters because importing it
    used to be enough to create an engine even in tests that never touch a
    database.
    """

    def __call__(self) -> AsyncSession:
        return get_session_factory()()

    def __getattr__(self, name: str):
        # Delegate anything else (e.g. `.kw`, `.begin`) to the real factory.
        return getattr(get_session_factory(), name)


session_factory = _LazySessionFactory()

__all__ = [
    "async_database_url",
    "get_session",
    "get_session_factory",
    "session_factory",
]

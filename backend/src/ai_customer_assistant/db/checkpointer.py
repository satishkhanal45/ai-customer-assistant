"""Durable LangGraph checkpointer construction (Phase 5, §4.5).

Replaces the `MemorySaver` used through Phases 0-4. Selection rule from the
plan: Postgres when the service is "multi-instance or already in the stack".
Deployment is docker-compose with a pgvector/pg16 `postgres` service and
every knowledge-base model is Postgres-backed (pgvector/JSONB), so Postgres
is the durable choice; `MemorySaver` remains only as a convenient dev/test
fallback when the `POSTGRES_*` env block is not configured.

```
await build_checkpointer()
```

returns a compiled checkpointer. For Postgres this is an `AsyncPostgresSaver`
(created and `setup()` once, backed by an `AsyncConnectionPool`), so the
compiled Supervisor graph must be driven with `ainvoke`, matching the async
requirement that already lands when the async Knowledge subgraph is wired.
"""
from __future__ import annotations

import os
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_REQUIRED_ENV = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")

# The app's own conversation-turn type stored in checkpoints. Registered so
# deserialization doesn't fall back to langgraph's permissive pickle path
# (which warns and is slated for removal).
_MSG_PACK_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("agents.contracts", "ConversationTurn"),
)


def _build_serde() -> JsonPlusSerializer:
    """Checkpoint serializer with the app's persisted types allowlisted."""
    return JsonPlusSerializer(allowed_msgpack_modules=_MSG_PACK_ALLOWLIST)


def postgres_dsn() -> Optional[str]:
    """Build a libpq conninfo string from the `POSTGRES_*` env block.

    Returns None (signal: no Postgres configured -> MemorySaver fallback)
    when any of the required vars is missing.
    """
    if not all(os.environ.get(name) for name in _REQUIRED_ENV):
        return None
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ["POSTGRES_DB"]
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def build_postgres_checkpointer(
    dsn: Optional[str] = None,
) -> BaseCheckpointSaver:
    """Construct an ``AsyncPostgresSaver`` over a connection pool and run
    ``setup()`` to create the checkpointer tables, resolving env lazily."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    resolved = dsn or postgres_dsn()
    if resolved is None:
        raise ValueError(
            "No Postgres DSN available; set POSTGRES_USER/POSTGRES_PASSWORD/"
            "POSTGRES_DB (and optionally POSTGRES_HOST/POSTGRES_PORT)."
        )

    pool = AsyncConnectionPool(conninfo=resolved, open=False, kwargs={"autocommit": True})
    await pool.open()
    saver = AsyncPostgresSaver(conn=pool, serde=_build_serde())
    await saver.setup()
    return saver


async def build_checkpointer() -> BaseCheckpointSaver:
    """Resolve the durable checkpointer: Postgres when configured, otherwise
    an in-memory `MemorySaver` for dev/test."""
    dsn = postgres_dsn()
    if dsn is None:
        return MemorySaver(serde=_build_serde())
    return await build_postgres_checkpointer(dsn)
"""Checkpointer selection tests (Phase 5, §4.5) — the CI story made explicit.

``db/checkpointer.build_checkpointer`` resolves the durable backend from the
environment:

  - ``POSTGRES_*`` configured  -> ``AsyncPostgresSaver`` (production path);
  - otherwise                  -> in-memory ``MemorySaver`` (dev/CI fallback).

These tests pin that contract on both sides:

  * the fallback path always runs (no infra needed) — CI without Postgres
    is *explicitly* the MemorySaver path, not an accident;
  * the real Postgres path is exercised whenever ``POSTGRES_*`` is present,
    and loudly skipped otherwise so nobody is fooled into thinking the
    durable path ran when it didn't.
"""
from __future__ import annotations

import pytest

from db.checkpointer import build_checkpointer, postgres_dsn


def test_fallback_is_memorysaver_when_no_postgres_env(monkeypatch):
    """CI without POSTGRES_* gets an in-memory checkpointer — asserted, not
    assumed. This is the documented dev/CI fallback path."""
    for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.delenv(var, raising=False)

    assert postgres_dsn() is None


@pytest.mark.asyncio
async def test_fallback_checkpointer_builds_memory_saver(monkeypatch):
    for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.delenv(var, raising=False)

    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = await build_checkpointer()
    assert isinstance(checkpointer, MemorySaver)


@pytest.mark.skipif(
    not postgres_dsn(),
    reason="POSTGRES_* not configured; skipping the real Postgres path",
)
@pytest.mark.asyncio
async def test_postgres_checkpointer_builds():
    """Real durable path: with POSTGRES_* set the saver is an
    AsyncPostgresSaver whose setup() created the checkpoint tables."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    checkpointer = await build_checkpointer()
    assert isinstance(checkpointer, AsyncPostgresSaver)
    # setup() ran inside build_postgres_checkpointer: probing an unknown
    # thread returns None instead of raising a missing-table error.
    recorded = await checkpointer.aget_tuple({"configurable": {"thread_id": "nope"}})
    assert recorded is None
    await checkpointer.conn.aclose()


def test_postgres_dsn_shape_with_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "d")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "5433")

    dsn = postgres_dsn()
    assert dsn == "postgresql://u:p@db:5433/d"
#!/usr/bin/env python
"""
One-off smoke test for ingestion/graph/queries.py, run directly against
your real database -- not a pytest test (no fixtures/teardown), just a
quick "does this actually work" check before wiring up an API around it.

    POSTGRES_HOST=localhost POSTGRES_PORT=5433 \\
    uv run --project backend python backend/scripts/test_graph_queries.py

Uses the entity IDs already visible in `SELECT * FROM entity`, so no setup
needed beyond having those rows present.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_PKG_ROOT = Path(__file__).resolve().parent.parent / "src" / "ai_customer_assistant"
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Real IDs from your `SELECT * FROM entity` output.
ANKUR = UUID("98221d4c-6617-4514-a4bc-a4e0c506922a")
F1SOFT = UUID("810043f9-dae3-4dd2-8e54-a95186495b76")
FONELOAN = UUID("b9e85e2b-39b1-407f-80ea-5aab3a78a7b6")
ALPINIST = UUID("21d4f4ab-f1c7-43fd-87bf-dd0af656e490")


async def main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.session import database_url
    from ingestion.graph.queries import find_entities, find_path, get_entity, get_neighbors

    engine = create_async_engine(database_url().replace("postgresql+psycopg://", "postgresql+psycopg://"))
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as session:
        print("=== get_entity(F1SOFT) ===")
        detail = await get_entity(session, ALPINIST)
        print(detail)

        print("\n=== get_entity(<random non-existent id>) -- should print None ===")
        missing = await get_entity(session, UUID("00000000-0000-0000-0000-000000000000"))
        print(missing)

        print("\n=== find_entities(entity_type='company') ===")
        companies = await find_entities(session, entity_type="company")
        print(companies)

        print("\n=== find_entities(name_query='fone') ===")
        fuzzy = await find_entities(session, name_query="fone")
        print(fuzzy)

        print("\n=== get_neighbors(ALPINIST, depth=1) -- expect just itself, relation table is empty ===")
        fragment = await get_neighbors(session, ALPINIST, depth=1)
        print("nodes:", fragment.nodes)
        print("edges:", fragment.edges)

        print("\n=== find_path(ANKUR, ALPINIST) -- expect None, no relations exist yet ===")
        path = await find_path(session, ANKUR, ALPINIST)
        print(path)

        print("\n=== find_path(ALPINIST, ALPINIST) -- same entity, should short-circuit ===")
        same = await find_path(session, ALPINIST, ALPINIST)
        print(same)

    await engine.dispose()
    print("\nDone -- no exceptions means the SQL/ORM shapes are at least valid against your schema.")


if __name__ == "__main__":
    asyncio.run(main())

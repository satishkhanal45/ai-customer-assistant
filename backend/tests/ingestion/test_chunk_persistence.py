"""Chunk persistence is idempotent, and the schema enforces it.

These pin the P0 fixes for the defect that failed 31 of the 82 ingestion
jobs in the development database. `persist_chunks` appended rather than
replaced, so re-ingesting a version wrote every chunk twice, and
`_link_entity_to_chunk`'s lookup by (version_id, chunk_index) then raised
"Multiple rows were found when exactly one was required". The state was
permanent: once a version held duplicates every later attempt failed the
same way, so the document could never be ingested again.

The test that matters most is `test_reingesting_a_version_replaces`: it
ingests the same version twice, which is precisely what nothing did before.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import (
    Base,
    EmbeddingChunk,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from ingestion.persistence import persist_chunks

SOURCE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
VERSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_VERSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _Chunk:
    """The shape persist_chunks reads off chunk_embed's EmbeddedChunk."""

    def __init__(self, index: int, text: str):
        self.chunk_index = index
        self.text = text
        self.token_count = len(text.split())
        self.metadata = {"page": 1}


class _Embedded:
    def __init__(self, index: int, text: str, embedding=None):
        self.chunk = _Chunk(index, text)
        self.embedding = embedding or [0.0] * 768


def _document(*texts: str) -> tuple:
    return tuple(_Embedded(i, text) for i, text in enumerate(texts))


@pytest_asyncio.fixture
async def session_factory():
    """SQLite carries the same UNIQUE constraint the migration adds to
    Postgres, so the constraint itself is under test rather than assumed."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            KnowledgeSource.__table__,
            KnowledgeSourceVersion.__table__,
            EmbeddingChunk.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(session_factory):
    async with session_factory() as session:
        for version_id in (VERSION_ID, OTHER_VERSION_ID):
            session.add(
                KnowledgeSourceVersion(
                    version_id=version_id,
                    source_id=SOURCE_ID,
                    version_number=1 if version_id == VERSION_ID else 2,
                    storage_uri=f"s3://x/{version_id}",
                    checksum=f"checksum-{version_id}",
                    status="PENDING",
                )
            )
        await session.commit()
    return session_factory


async def _count(session, version_id=VERSION_ID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(EmbeddingChunk)
                .where(EmbeddingChunk.version_id == version_id)
            )
        ).scalar_one()
    )


class TestIdempotency:
    async def test_writes_one_row_per_chunk(self, seeded):
        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, _document("alpha", "beta"), {})
            await session.commit()
            assert await _count(session) == 2

    async def test_reingesting_a_version_replaces(self, seeded):
        """The regression. Two runs over the same version must leave the
        chunks of one run, not both."""
        document = _document("alpha", "beta", "gamma")

        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, document, {})
            await session.commit()

        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, document, {})
            await session.commit()
            assert await _count(session) == 3

    async def test_three_runs_still_leave_one_copy(self, seeded):
        """The observed data had up to seven copies of one index, so once is
        not enough to prove the append is gone."""
        document = _document("alpha", "beta")
        for _ in range(3):
            async with seeded() as session:
                await persist_chunks(session, VERSION_ID, document, {})
                await session.commit()

        async with seeded() as session:
            assert await _count(session) == 2

    async def test_a_shorter_document_leaves_no_orphans(self, seeded):
        """Replacing means replacing. A re-ingest that produces fewer chunks
        must not leave the tail of the previous run behind, which an
        upsert-by-index would."""
        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, _document("a", "b", "c", "d"), {})
            await session.commit()

        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, _document("a", "b"), {})
            await session.commit()
            assert await _count(session) == 2

    async def test_other_versions_are_untouched(self, seeded):
        """The delete is scoped to one version_id. Superseded versions of the
        same source keep their chunks -- they are a different snapshot, not
        a duplicate (see status.md P1-7 for whether they stay searchable)."""
        async with seeded() as session:
            await persist_chunks(session, OTHER_VERSION_ID, _document("kept", "also kept"), {})
            await persist_chunks(session, VERSION_ID, _document("first"), {})
            await session.commit()

        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, _document("second"), {})
            await session.commit()
            assert await _count(session, OTHER_VERSION_ID) == 2
            assert await _count(session, VERSION_ID) == 1


class TestConstraint:
    async def test_the_database_refuses_a_duplicate_index(self, seeded):
        """Belt and braces: the write path is idempotent, and the schema
        makes the broken state unrepresentable regardless of what any future
        writer does."""
        async with seeded() as session:
            for _ in range(2):
                session.add(
                    EmbeddingChunk(
                        version_id=VERSION_ID,
                        chunk_index=0,
                        text="collision",
                        embedding=[0.0] * 768,
                        checksum="c",
                    )
                )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_the_same_index_in_another_version_is_fine(self, seeded):
        """The constraint is on the pair, not on chunk_index alone."""
        async with seeded() as session:
            for version_id in (VERSION_ID, OTHER_VERSION_ID):
                session.add(
                    EmbeddingChunk(
                        version_id=version_id,
                        chunk_index=0,
                        text="same index, different version",
                        embedding=[0.0] * 768,
                        checksum=f"c-{version_id}",
                    )
                )
            await session.commit()
            assert await _count(session, VERSION_ID) == 1
            assert await _count(session, OTHER_VERSION_ID) == 1


class TestEmbeddingReuse:
    async def test_a_matching_checksum_reuses_the_previous_embedding(self, seeded):
        """Unchanged text keeps its vector instead of being re-embedded --
        the optimisation the replace-don't-append change must not break.

        Read back as raw SQL rather than through the ORM: the embedding
        column is pgvector's `Vector` with a JSON variant for SQLite, and
        loading a whole mapped row through that variant mis-maps the result
        columns off Postgres. The stored value is what this test is about,
        so it reads exactly that.
        """
        import json

        from sqlalchemy import text as sql_text

        from ingestion.persistence import compute_chunk_checksum

        content = "unchanged paragraph"
        reused_vector = tuple([0.5] * 768)
        reuse = {compute_chunk_checksum(content): reused_vector}

        async with seeded() as session:
            await persist_chunks(session, VERSION_ID, _document(content), reuse)
            await session.commit()
            stored = (
                await session.execute(
                    sql_text(
                        "SELECT embedding FROM embedding_chunk WHERE version_id = :v"
                    ),
                    {"v": VERSION_ID.hex},
                )
            ).scalar_one()

        assert json.loads(stored)[:3] == [0.5, 0.5, 0.5]

    async def test_a_new_checksum_keeps_the_freshly_computed_vector(self, seeded):
        """The other half: text that changed must not pick up a stale
        embedding just because some other chunk reused one."""
        import json

        from sqlalchemy import text as sql_text

        async with seeded() as session:
            await persist_chunks(
                session, VERSION_ID, _document("brand new text"), {"unrelated": (0.5,) * 768}
            )
            await session.commit()
            stored = (
                await session.execute(
                    sql_text("SELECT embedding FROM embedding_chunk WHERE version_id = :v"),
                    {"v": VERSION_ID.hex},
                )
            ).scalar_one()

        assert json.loads(stored)[:3] == [0.0, 0.0, 0.0]

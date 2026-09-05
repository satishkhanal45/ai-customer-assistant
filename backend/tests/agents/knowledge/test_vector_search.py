"""Tests for agents/knowledge/vector_search.py.

The module had no tests at all, which is how the "fetch every embedding
and rank in Python" behaviour survived unexamined. These cover the two
things that matter now that ranking is dialect-aware:

  * the pure scoring helpers still behave (they back the SQLite path);
  * the pgvector path emits the SQL it claims to - ORDER BY on the
    cosine-distance operator, LIMIT top_k, a distance threshold derived
    from the similarity threshold, and crucially *no* embedding column,
    since not shipping 768 floats per row is the entire point.

The SQLite end-to-end test proves the join contract and the Python
ranking still agree on real rows without needing a live Postgres.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agents.knowledge import vector_search as vs
from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.exceptions import EmptyRetrievalError, VectorSearchError
from agents.knowledge.types import RewrittenQuery

DIM = 4


def _config(**overrides) -> KnowledgeAgentConfig:
    base = dict(embedding_dimension=DIM, top_k=3, max_context_chunks=12, similarity_threshold=0.5)
    base.update(overrides)
    return KnowledgeAgentConfig(**base)


def _query(text: str = "how do refunds work") -> RewrittenQuery:
    return RewrittenQuery(original_text=text, rewritten_text=text)


# ==========================================================================
# Pure helpers (the SQLite path)
# ==========================================================================


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self) -> None:
        assert vs._cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        assert vs._cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)

    def test_zero_vector_degrades_to_zero_not_a_crash(self) -> None:
        """A stored all-zero embedding is an ingestion data-quality problem,
        not something retrieval should raise on."""
        assert vs._cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0

    def test_parses_embedding_from_json_text_or_sequence(self) -> None:
        assert vs._parse_embedding("[1.0, 2.0]") == (1.0, 2.0)
        assert vs._parse_embedding([1.0, 2.0]) == (1.0, 2.0)


class TestEmbedQueryText:
    def test_wrong_dimension_is_rejected(self) -> None:
        with pytest.raises(VectorSearchError, match="dimension"):
            vs._embed_query_text(lambda _t: [0.1] * (DIM + 1), "q", config=_config())

    def test_backend_failure_is_wrapped(self) -> None:
        def boom(_t):
            raise RuntimeError("model down")

        with pytest.raises(VectorSearchError, match="embedding failed"):
            vs._embed_query_text(boom, "q", config=_config())


# ==========================================================================
# The pgvector path — SQL shape
# ==========================================================================


def _compiled_pgvector_sql(config: KnowledgeAgentConfig) -> str:
    stmt = vs._pgvector_ranked_statement(tuple([0.1] * DIM), config=config)
    return str(stmt.compile(dialect=postgresql.dialect()))


class TestPgvectorStatement:
    def test_orders_by_the_cosine_distance_operator(self) -> None:
        sql = _compiled_pgvector_sql(_config())
        assert "<=>" in sql
        assert "ORDER BY" in sql.upper()

    def test_limits_to_top_k_in_sql(self) -> None:
        stmt = vs._pgvector_ranked_statement(tuple([0.1] * DIM), config=_config(top_k=3))
        assert stmt._limit == 3

    def test_does_not_select_the_embedding_column(self) -> None:
        """The whole point: the database scores, so 768 floats per row must
        never cross the wire."""
        columns = [c.name for c in vs._pgvector_ranked_statement(
            tuple([0.1] * DIM), config=_config()
        ).selected_columns]
        assert "embedding" not in columns
        assert "distance" in columns

    def test_similarity_threshold_becomes_a_distance_bound(self) -> None:
        """<=> is cosine *distance*, so a 0.75 similarity floor is a 0.25
        distance ceiling. Getting this backwards would silently invert the
        filter and return the least relevant chunks."""
        params = vs._pgvector_ranked_statement(
            tuple([0.1] * DIM), config=_config(similarity_threshold=0.75)
        ).compile(dialect=postgresql.dialect()).params
        assert any(
            isinstance(v, float) and v == pytest.approx(0.25) for v in params.values()
        ), params

    def test_preserves_the_live_version_join_contract(self) -> None:
        """Pushing the ranking into SQL must not quietly drop the filters
        that keep stale or withdrawn documents out of retrieval."""
        compiled = vs._pgvector_ranked_statement(
            tuple([0.1] * DIM), config=_config()
        ).compile(dialect=postgresql.dialect())
        sql = str(compiled)

        assert "current_version_id" in sql
        assert "is_active" in sql
        assert "INDEXED" in compiled.params.values()

    def test_candidate_statement_includes_embedding_for_the_python_path(self) -> None:
        columns = [c.name for c in vs._candidate_chunks_statement().selected_columns]
        assert "embedding" in columns


class TestDialectDetection:
    def test_non_postgres_session_takes_the_python_path(self) -> None:
        class _Bind:
            dialect = type("D", (), {"name": "sqlite"})()

        class _Session:
            def get_bind(self):
                return _Bind()

        assert vs._uses_pgvector(_Session()) is False

    def test_postgres_session_takes_the_pgvector_path(self) -> None:
        class _Bind:
            dialect = type("D", (), {"name": "postgresql"})()

        class _Session:
            def get_bind(self):
                return _Bind()

        assert vs._uses_pgvector(_Session()) is True

    def test_uninspectable_session_degrades_to_the_portable_path(self) -> None:
        class _Session:
            def get_bind(self):
                raise RuntimeError("no bind")

        assert vs._uses_pgvector(_Session()) is False


# ==========================================================================
# End to end on SQLite — join contract + Python ranking
# ==========================================================================


@pytest.fixture
async def sqlite_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(vs.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(session, *, status="INDEXED", is_active=True, current=True) -> None:
    """One source -> one version -> two chunks pointing in different directions."""
    await session.execute(
        vs.knowledge_source_table.insert().values(
            source_id="s1", category_id=None, source_name="handbook",
            source_type="FILE_UPLOAD", origin_system=None, external_reference_id=None,
            uploaded_by=None, current_version_id="v1" if current else "other", is_active=is_active,
        )
    )
    await session.execute(
        vs.knowledge_source_version_table.insert().values(
            version_id="v1", source_id="s1", version_number=3, file_type="MD", status=status,
        )
    )
    for chunk_id, vector, idx in (
        ("c-near", [1.0, 0.0, 0.0, 0.0], 0),
        ("c-far", [0.0, 1.0, 0.0, 0.0], 1),
    ):
        await session.execute(
            vs.embedding_chunk_table.insert().values(
                chunk_id=chunk_id, version_id="v1", entity_id=None, chunk_index=idx,
                text=f"body of {chunk_id}", embedding=json.dumps(vector), page=idx + 1,
            )
        )
    await session.commit()


class TestSqliteEndToEnd:
    async def test_returns_only_chunks_over_the_threshold_ranked_best_first(self, sqlite_session):
        await _seed(sqlite_session)
        chunks = await vs.vector_search(
            _query(), config=_config(similarity_threshold=0.5),
            session=sqlite_session, embed_query=lambda _t: [1.0, 0.0, 0.0, 0.0],
        )
        assert [c.chunk_id for c in chunks] == ["c-near"]
        assert chunks[0].similarity_score == pytest.approx(1.0)
        assert chunks[0].chunk_text == "body of c-near"

    async def test_provenance_is_resolved_from_the_join(self, sqlite_session):
        await _seed(sqlite_session)
        (chunk,) = await vs.vector_search(
            _query(), config=_config(similarity_threshold=0.5),
            session=sqlite_session, embed_query=lambda _t: [1.0, 0.0, 0.0, 0.0],
        )
        assert chunk.provenance.source_name == "handbook"
        assert chunk.provenance.version_number == 3
        assert chunk.provenance.page == 1
        assert chunk.provenance.category_name is None

    async def test_nothing_over_threshold_raises_empty_retrieval(self, sqlite_session):
        await _seed(sqlite_session)
        with pytest.raises(EmptyRetrievalError):
            await vs.vector_search(
                _query(), config=_config(similarity_threshold=0.99),
                session=sqlite_session, embed_query=lambda _t: [0.0, 0.0, 1.0, 0.0],
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"status": "PENDING"},   # not yet indexed
            {"is_active": False},    # source soft-deleted
            {"current": False},      # superseded by a newer version
        ],
        ids=["not_indexed", "inactive_source", "not_current_version"],
    )
    async def test_join_contract_excludes_non_live_content(self, sqlite_session, kwargs):
        """A reindex in progress, an archived source, or a stale version must
        never surface — otherwise the assistant cites withdrawn documents."""
        await _seed(sqlite_session, **kwargs)
        with pytest.raises(EmptyRetrievalError):
            await vs.vector_search(
                _query(), config=_config(similarity_threshold=0.5),
                session=sqlite_session, embed_query=lambda _t: [1.0, 0.0, 0.0, 0.0],
            )

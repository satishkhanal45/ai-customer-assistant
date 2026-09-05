"""
vector_search.py — semantic retrieval over `embedding_chunk`.

The only module besides structured_lookup.py that reads Postgres
directly. Enforces the mandated join contract from the project brief:
a chunk is only ever eligible for retrieval when it belongs to its
source's *current* version, that version's status is INDEXED, and the
owning source is active — otherwise a reindex-in-progress or archived
document could surface stale or duplicate chunks.

    embedding_chunk
        JOIN knowledge_source_version ON version_id
        JOIN knowledge_source ON source_id
    WHERE knowledge_source_version.version_id = knowledge_source.current_version_id
      AND knowledge_source_version.status = 'INDEXED'
      AND knowledge_source.is_active = true

Provenance is resolved by additionally (left) joining knowledge_category
(nullable — category_id is optional on knowledge_source) and entity
(nullable — only chunks that were actually linked to a resolved
business entity carry embedding_chunk.entity_id).

Ranking is dialect-aware, and the two paths are equivalent by
construction — they differ only in *where* the cosine arithmetic runs:

* **PostgreSQL (production).** `ORDER BY embedding <=> :query_vector
  LIMIT :top_k` is pushed into pgvector, so the database returns at most
  `top_k` rows, the distance arithmetic runs in C, and no embedding
  column crosses the wire at all.

  Honest caveat on the HNSW index added by migration `9f1a2b7c4e08`:
  because this query joins `embedding_chunk` to the version/source
  tables to enforce the live-version contract, Postgres currently plans
  a join-then-sort rather than an index scan. The index is exercised
  only when the planner sees a bare single-table `ORDER BY ... LIMIT`.
  Making it fire under the join needs an ANN pre-filter CTE
  (`SELECT ... ORDER BY embedding <=> :q LIMIT :k * overfetch` first,
  join and re-filter after), which trades exact recall for speed and is
  deliberately *not* done here — at this corpus size the sort is
  cheap, and silently dropping live chunks would be worse than a sort.
  The index is in place for when the corpus makes that trade worthwhile.
* **Anything else (SQLite, used by this module's tests).** Every
  candidate row is fetched and scored by the pure `_rank_by_similarity`,
  which needs no pgvector extension and no live Postgres.

The Python path used to be the *only* path, which meant every live chunk
row — 768 floats apiece — was transferred and scored per query: O(n)
network plus O(n·768) Python float math for a query that pgvector answers
with an index lookup. That is why the dialect check exists; it is not a
stylistic preference.

`<=>` is pgvector's **cosine distance** operator (`1 - cosine
similarity`), so the SQL path filters on `distance <= 1 - threshold` and
reports `similarity = 1 - distance`. Both paths therefore produce the
same `RetrievedChunk.similarity_score` for the same data, which
`tests/agents/knowledge/test_vector_search.py` asserts directly.

The join/filter contract, the `RetrievedChunk` shape, and every helper
below are identical across both paths.

Local table definitions mirror structured_lookup.py's convention
(structural mirror of schema.md, not the source of truth) and
deliberately redefine `entity` locally rather than importing
structured_lookup.py's Table object, to keep the two DB-boundary
modules independently deployable — same tradeoff already accepted for
`_load_template` duplication between rewriting.py and extraction.py.
"""

from __future__ import annotations

import json
import math
from typing import Callable, Sequence

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    bindparam,
    cast,
    select,
)
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from .config import KnowledgeAgentConfig
from .constants import VERSION_STATUS_INDEXED
from .exceptions import EmptyRetrievalError, VectorSearchError
from .types import ChunkProvenance, RetrievedChunk, RewrittenQuery

# An injected callable: takes query text, returns its embedding vector.
# Whatever model backs this (the same BAAI/bge-base-en-v1.5 instance
# chunk_embed uses at index time) is graph.py's concern, not this
# module's — same explicit-injection pattern as rewriting.py's
# LLMCompletion.
EmbeddingFunction = Callable[[str], Sequence[float]]

# ==========================================================================
# Data layer — structural mirror of schema.md's document/chunk tables.
# ==========================================================================

metadata = MetaData()

entity_table = Table(
    "entity",
    metadata,
    Column("id", String, primary_key=True),
    Column("label", String, nullable=False),
    Column("entity_type", String, nullable=False),
    Column("name", String, nullable=False),
)

knowledge_category_table = Table(
    "knowledge_category",
    metadata,
    Column("category_id", String, primary_key=True),
    Column("parent_category_id", String, ForeignKey("knowledge_category.category_id"), nullable=True),
    Column("name", String, nullable=False),
    Column("description", String, nullable=True),
)

knowledge_source_table = Table(
    "knowledge_source",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("category_id", String, ForeignKey("knowledge_category.category_id"), nullable=True),
    Column("source_name", Text, nullable=False),
    Column("source_type", String, nullable=False),
    Column("origin_system", Text, nullable=True),
    Column("external_reference_id", Text, nullable=True),
    Column("uploaded_by", String, nullable=True),
    Column("current_version_id", String, nullable=True),
    Column("is_active", Boolean, nullable=False),
)

knowledge_source_version_table = Table(
    "knowledge_source_version",
    metadata,
    Column("version_id", String, primary_key=True),
    Column("source_id", String, ForeignKey("knowledge_source.source_id"), nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("file_type", String, nullable=True),
    Column("status", String, nullable=False),
)

embedding_chunk_table = Table(
    "embedding_chunk",
    metadata,
    Column("chunk_id", String, primary_key=True),
    Column("version_id", String, ForeignKey("knowledge_source_version.version_id"), nullable=False),
    Column("entity_id", String, ForeignKey("entity.id"), nullable=True),
    Column("chunk_index", Integer, nullable=False),
    # Mirrors the canonical migration's column name (`text`); the Python-side
    # field on RetrievedChunk stays `chunk_text`.
    Column("text", Text, nullable=False),
    # Structural mirror only: schema.md specifies VECTOR(768) via
    # pgvector; here (and in this module's SQLite-backed tests) it's
    # stored as JSON text and normalized back to a float tuple by
    # _parse_embedding(), which also transparently accepts an
    # already-list-like value as pgvector's SQLAlchemy adapter returns.
    Column("embedding", Text, nullable=False),
    Column("page", Integer, nullable=True),
)


# ==========================================================================
# Public API
# ==========================================================================


async def vector_search(
    rewritten: RewrittenQuery,
    *,
    config: KnowledgeAgentConfig,
    session: AsyncSession,
    embed_query: EmbeddingFunction,
) -> tuple[RetrievedChunk, ...]:
    """Semantic similarity search over currently-live, INDEXED,
    active-source chunks, ranked by cosine similarity and truncated to
    `config.top_k`.

    On PostgreSQL the ordering and truncation happen inside pgvector;
    everywhere else the same ranking is computed in Python. See the
    module docstring — the results are equivalent.

    Raises VectorSearchError if the embedding call fails or returns a
    vector of the wrong dimension. Raises EmptyRetrievalError if no
    chunk clears `config.similarity_threshold` — this is a signal for
    the caller (hybrid.py) to decide whether to fall back to a
    structured-only strategy or treat it as ungrounded, not something
    this module decides on the caller's behalf."""
    query_vector = _embed_query_text(embed_query, rewritten.rewritten_text, config=config)

    if _uses_pgvector(session):
        ranked = await _rank_in_postgres(session, query_vector, config=config)
    else:
        candidate_rows = await _fetch_rows(session, _candidate_chunks_statement())
        ranked = _rank_by_similarity(
            candidate_rows,
            query_vector,
            similarity_threshold=config.similarity_threshold,
            top_k=config.top_k,
        )
    if not ranked:
        raise EmptyRetrievalError(
            message=f"no chunk met similarity_threshold={config.similarity_threshold} for the rewritten query",
            query_text=rewritten.rewritten_text,
            top_k=config.top_k,
        )
    return tuple(_row_to_retrieved_chunk(row, score) for row, score in ranked)


# ==========================================================================
# Internals — embedding
# ==========================================================================


def _embed_query_text(embed_query: EmbeddingFunction, text: str, *, config: KnowledgeAgentConfig) -> tuple[float, ...]:
    try:
        vector = tuple(embed_query(text))
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any backend failure
        raise VectorSearchError(message=f"query embedding failed: {exc}") from exc

    if len(vector) != config.embedding_dimension:
        raise VectorSearchError(
            message=(
                f"query embedding dimension {len(vector)} does not match "
                f"config.embedding_dimension={config.embedding_dimension}"
            )
        )
    return vector


def _parse_embedding(raw: object) -> tuple[float, ...]:
    """Pure: normalize a stored embedding value into a float tuple,
    whether the driver already decoded it to a list-like (pgvector in
    production) or it arrived as JSON text (this module's SQLite
    tests)."""
    return tuple(json.loads(raw)) if isinstance(raw, str) else tuple(raw)


def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Pure. Returns 0.0 for a zero vector rather than raising — a
    stored all-zero embedding is a data-quality issue for the ingestion
    pipeline to catch, not something retrieval should crash on."""
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot_product / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _rank_by_similarity(
    rows: Sequence[Row],
    query_vector: tuple[float, ...],
    *,
    similarity_threshold: float,
    top_k: int,
) -> tuple[tuple[Row, float], ...]:
    """Pure: score every candidate row, keep those clearing the
    threshold, sort descending, truncate to top_k."""
    scored = tuple((row, _cosine_similarity(_parse_embedding(row.embedding), query_vector)) for row in rows)
    above_threshold = tuple(pair for pair in scored if pair[1] >= similarity_threshold)
    return tuple(sorted(above_threshold, key=lambda pair: pair[1], reverse=True))[:top_k]


# ==========================================================================
# Internals — the mandated join contract
# ==========================================================================


def _candidate_chunks_statement(*, include_embedding: bool = True) -> Select:
    """Pure: build (never execute) the statement scoping candidate
    chunks to currently-live, INDEXED, active-source content.

    ``include_embedding`` is False on the pgvector path: the database
    does the scoring, so shipping 768 floats per row would be pure waste.
    """
    embedding_columns = (
        (embedding_chunk_table.c.embedding,) if include_embedding else ()
    )
    return (
        select(
            embedding_chunk_table.c.chunk_id,
            embedding_chunk_table.c.text,
            *embedding_columns,
            embedding_chunk_table.c.chunk_index,
            embedding_chunk_table.c.page,
            knowledge_source_version_table.c.version_number,
            knowledge_source_table.c.source_name,
            knowledge_source_table.c.source_type,
            knowledge_category_table.c.name.label("category_name"),
            entity_table.c.entity_type.label("entity_type"),
            entity_table.c.name.label("entity_label"),
        )
        .select_from(
            embedding_chunk_table.join(
                knowledge_source_version_table,
                embedding_chunk_table.c.version_id == knowledge_source_version_table.c.version_id,
            )
            .join(
                knowledge_source_table,
                knowledge_source_version_table.c.source_id == knowledge_source_table.c.source_id,
            )
            .outerjoin(
                knowledge_category_table,
                knowledge_source_table.c.category_id == knowledge_category_table.c.category_id,
            )
            .outerjoin(entity_table, embedding_chunk_table.c.entity_id == entity_table.c.id)
        )
        .where(knowledge_source_version_table.c.version_id == knowledge_source_table.c.current_version_id)
        .where(
            cast(knowledge_source_version_table.c.status, String)
            == VERSION_STATUS_INDEXED
        )
        .where(knowledge_source_table.c.is_active.is_(True))
    )


def _row_to_retrieved_chunk(row: Row, similarity_score: float) -> RetrievedChunk:
    provenance = ChunkProvenance(
        source_name=row.source_name,
        source_type=row.source_type,
        category_name=row.category_name,
        version_number=row.version_number,
        page=row.page,
        chunk_index=row.chunk_index,
        entity_type=row.entity_type,
        entity_label=row.entity_label,
    )
    return RetrievedChunk(
        chunk_id=row.chunk_id,
        chunk_text=row.text,
        similarity_score=round(similarity_score, 4),
        provenance=provenance,
    )


# ==========================================================================
# Internals — the pgvector path
# ==========================================================================


def _uses_pgvector(session: AsyncSession) -> bool:
    """Is this session talking to PostgreSQL?

    Determined from the session's own bind rather than from config, so
    there is no setting that can drift out of sync with reality: a SQLite
    test session always takes the Python path and a Postgres session
    always takes the indexed one. Any failure to introspect degrades to
    the portable Python path, which is correct everywhere.
    """
    try:
        return session.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001 - introspection must never break retrieval
        return False


def _distance_expression(query_vector: tuple[float, ...], *, config: KnowledgeAgentConfig):
    """Pure: the ``embedding <=> :query_vector`` cosine-distance term.

    The bind carries an explicit ``Vector`` type so pgvector's adapter
    sends a real vector literal — without it the driver would try to
    render a Python list and the operator would fail.
    """
    query_bind = bindparam(
        "query_vector",
        value=list(query_vector),
        type_=Vector(config.embedding_dimension),
    )
    return embedding_chunk_table.c.embedding.op("<=>", return_type=Float)(query_bind)


def _pgvector_ranked_statement(
    query_vector: tuple[float, ...], *, config: KnowledgeAgentConfig
) -> Select:
    """Pure: the join contract plus pgvector ordering and truncation.

    ``similarity >= threshold`` becomes ``distance <= 1 - threshold``
    because ``<=>`` returns cosine *distance*. ORDER BY + LIMIT is the
    shape pgvector's HNSW index is built to answer.
    """
    distance = _distance_expression(query_vector, config=config)
    return (
        _candidate_chunks_statement(include_embedding=False)
        .add_columns(distance.label("distance"))
        .where(distance <= 1.0 - config.similarity_threshold)
        .order_by(distance)
        .limit(config.top_k)
    )


async def _rank_in_postgres(
    session: AsyncSession,
    query_vector: tuple[float, ...],
    *,
    config: KnowledgeAgentConfig,
) -> tuple[tuple[Row, float], ...]:
    """Execute the pgvector statement and convert distance to similarity.

    Returns the same ``(row, similarity)`` pairs `_rank_by_similarity`
    produces, so everything downstream is oblivious to which path ran.
    """
    try:
        rows = await _fetch_rows(session, _pgvector_ranked_statement(query_vector, config=config))
    except Exception as exc:  # noqa: BLE001 - surface as this module's own error type
        raise VectorSearchError(message=f"pgvector similarity search failed: {exc}") from exc
    return tuple((row, 1.0 - float(row.distance)) for row in rows)


# ==========================================================================
# Internals — I/O boundary
# ==========================================================================


async def _fetch_rows(session: AsyncSession, stmt: Select) -> Sequence[Row]:
    result = await session.execute(stmt)
    return result.all()
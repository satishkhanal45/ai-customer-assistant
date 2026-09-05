"""add HNSW index on embedding_chunk.embedding

Retrieval now pushes `ORDER BY embedding <=> :query_vector LIMIT :top_k`
into pgvector instead of scoring every live chunk in Python.

`vector_cosine_ops` matches the `<=>` operator the query uses. Embeddings
are written normalized (IngestionSettings.normalize_embeddings), so cosine
is the right distance for this corpus.

Note: the retrieval query joins embedding_chunk to knowledge_source_version
and knowledge_source to enforce the live-version contract, and with that
join Postgres currently plans a join-then-sort rather than an HNSW index
scan. The index is still worth having: it is what any future ANN
pre-filter (a `LIMIT`-ed CTE over embedding_chunk alone, joined
afterwards) would ride on, and the planner will reach for it as the
corpus grows. See vector_search.py's module docstring.

Built CONCURRENTLY so an existing deployment is not locked out of writes
while the index builds. That requires running outside a transaction, hence
the autocommit block.

Revision ID: 9f1a2b7c4e08
Revises: 7b3e5c1a9d42
Create Date: 2026-09-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "9f1a2b7c4e08"
down_revision: Union[str, None] = "7b3e5c1a9d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_embedding_chunk_embedding_hnsw"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON embedding_chunk USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")

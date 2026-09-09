"""Deduplicate embedding_chunk, then make the duplicate state impossible.

`ingestion.persistence.persist_chunks` appended rather than replaced, so
re-ingesting a version wrote every chunk a second time. The lookup by
(version_id, chunk_index) in `_link_entity_to_chunk` then failed with
"Multiple rows were found when exactly one was required" -- 31 of the 82
ingestion jobs in the development database, and the state was permanent:
once a version held duplicates, every later attempt failed identically and
the document could never be ingested again.

The write path is fixed. This migration deals with the rows already there
and then makes the state unrepresentable.

**Order matters.** The delete has to run before the constraint is added, or
creating the constraint fails against exactly the data it exists to prevent.

**Which row survives.** The lowest `chunk_id` per (version_id, chunk_index).
Every duplicate group was produced by re-ingesting identical bytes -- the
pipeline verifies the version's checksum before chunking -- so the rows in a
group carry the same text and the same embedding, and the choice only
decides which primary key remains.

Rows in `knowledge_source_entity_map` are not touched: they key on
(version_id, entity_id), not on chunk, so nothing there dangles.

Revision ID: b7d1e93a5c40
Revises: 9a4f7c2b83d1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d1e93a5c40"
down_revision: Union[str, None] = "9a4f7c2b83d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    duplicates = connection.execute(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT version_id, chunk_index
                FROM embedding_chunk
                GROUP BY version_id, chunk_index
                HAVING count(*) > 1
            ) AS d
            """
        )
    ).scalar_one()

    if duplicates:
        # A single statement rather than a loop: the set of rows to delete is
        # decidable in SQL, and doing it row by row would leave the table
        # half-deduplicated if the migration were interrupted.
        removed = connection.execute(
            sa.text(
                """
                DELETE FROM embedding_chunk
                WHERE chunk_id IN (
                    SELECT chunk_id FROM (
                        SELECT chunk_id,
                               row_number() OVER (
                                   PARTITION BY version_id, chunk_index
                                   ORDER BY chunk_id
                               ) AS rn
                        FROM embedding_chunk
                    ) ranked
                    WHERE ranked.rn > 1
                )
                """
            )
        ).rowcount
        print(
            f"embedding_chunk: {duplicates} duplicated (version_id, chunk_index) "
            f"group(s), {removed} redundant row(s) removed."
        )

    op.create_unique_constraint(
        "uq_chunk_version_index", "embedding_chunk", ["version_id", "chunk_index"]
    )


def downgrade() -> None:
    # Only the constraint comes back off. The deleted rows were redundant
    # copies; recreating them is neither possible nor desirable.
    op.drop_constraint("uq_chunk_version_index", "embedding_chunk", type_="unique")

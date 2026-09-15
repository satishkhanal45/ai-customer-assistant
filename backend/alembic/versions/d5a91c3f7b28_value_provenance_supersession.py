"""Fact provenance and supersession (ingestion.md P1 item 5).

The two retrieval paths answered "is this still true?" in opposite, and both
wrong, ways. Chunks kept history and hid it: `cutover` marks the old version
STALE and deletes nothing, but `vector_search` filters to
`current_version_id`, so a superseded passage is unreachable. Facts kept
history and could not distinguish it: `value` rows are written with ON
CONFLICT DO NOTHING on (entity, attribute, value), so when a price changes
from $500 to $800 **both rows survive**, under the same entity and attribute,
with no version link and nothing marking which is current.

The second is the more serious: a missing answer is visibly missing, whereas
two contradictory prices presented as equally true is a *wrong* answer
delivered confidently.

The decision taken is to **keep history and mark it**, rather than to prune
it -- the rate that used to apply is a real fact, and deleting it makes "what
was the previous rate?" permanently unanswerable. Retrieval filters to
current values, so the contradiction stops; the history accumulates for a
temporal feature that can be built later. The chunk half is deliberately left
alone: semantic search continues to see only current versions.

`value_provenance` is a link table rather than a column on `value`, because a
`value` row is global -- the same fact is often stated by several documents,
and storing it once is what keeps "Alpinist Studios employs Justin Flores"
from appearing five times. A single `version_id` column could not express
that, and without it supersession is not decidable: a fact dropped by one
document may still be asserted by another.

No backfill. Which version asserted which existing fact is not recoverable,
and `resolve_superseded_values` never touches a value with no provenance at
all -- so every row written before today stays current, exactly as it was.
History starts accumulating from the next ingestion.

Revision ID: d5a91c3f7b28
Revises: c4f8b2e17a90
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5a91c3f7b28"
down_revision: Union[str, None] = "c4f8b2e17a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "value", sa.Column("superseded_at", sa.DateTime(), nullable=True)
    )
    # Retrieval filters on this on every structured lookup, and the
    # overwhelming majority of rows are current (NULL), so the index earns
    # its place on the selective side.
    op.create_index("ix_value_superseded_at", "value", ["superseded_at"])

    op.create_table(
        "value_provenance",
        sa.Column("value_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["value_id"], ["value.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["knowledge_source_version.version_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("value_id", "version_id"),
    )
    # The currency pass asks "which values does this version assert?" and
    # "which versions assert this value?". The primary key serves the first;
    # this index serves the second.
    op.create_index(
        "ix_value_provenance_version", "value_provenance", ["version_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_value_provenance_version", table_name="value_provenance")
    op.drop_table("value_provenance")
    op.drop_index("ix_value_superseded_at", table_name="value")
    op.drop_column("value", "superseded_at")

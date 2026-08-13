"""dedupe relation/value rows and enforce uniqueness

Relation rows have no unique constraint, and relation/value writes were not
idempotent -- the same fact or relation extracted again (re-ingestion, or
the CLI/worker double-consuming the same job) produced duplicate rows (e.g.
"Alpinist Studios employs Justin Flores" twice).

This migration:
  1. collapses existing duplicates (keeps the oldest row per unique triple),
  2. adds unique constraints matching the new ON CONFLICT DO NOTHING writes
     in ingestion/persistence.py.

Revision ID: 2a1c9f0e45d7
Revises: 1e4beb1b8d68
Create Date: 2026-08-13 12:30:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "2a1c9f0e45d7"
down_revision: Union[str, None] = "1e4beb1b8d68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep one row per (source_entity_id, target_entity_id, relation_type) --
    # the lowest id is the first one written.
    op.execute(
        """
        DELETE FROM relation a USING relation b
        WHERE a.id > b.id
          AND a.source_entity_id = b.source_entity_id
          AND a.target_entity_id = b.target_entity_id
          AND a.relation_type = b.relation_type
        """
    )
    op.create_unique_constraint(
        "uq_relation_source_target_type",
        "relation",
        ["source_entity_id", "target_entity_id", "relation_type"],
    )

    # Keep one row per (entity_id, attribute_id, value).
    op.execute(
        """
        DELETE FROM "value" a USING "value" b
        WHERE a.id > b.id
          AND a.entity_id = b.entity_id
          AND a.attribute_id = b.attribute_id
          AND a.value = b.value
        """
    )
    op.create_unique_constraint(
        "uq_value_entity_attribute_value",
        "value",
        ["entity_id", "attribute_id", "value"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_value_entity_attribute_value", "value", type_="unique")
    op.drop_constraint("uq_relation_source_target_type", "relation", type_="unique")

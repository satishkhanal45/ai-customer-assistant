"""enable required postgres extensions

Revision ID: 0001
Revises:
Create Date: 2026-08-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gen_random_uuid() -- used as the default for every UUID primary key.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # VECTOR(n) column type -- used by embedding_chunk.embedding.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    raise NotImplementedError("Rollback not implemented yet -- restore from a backup instead.")
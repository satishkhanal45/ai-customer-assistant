"""add deferred FK: knowledge_source.current_version_id -> knowledge_source_version

Revision ID: 0003
Revises: <PASTE_YOUR_HEAD_REVISION_ID_HERE>
Create Date: 2026-08-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "11160c9078cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_knowledge_source_current_version",
        "knowledge_source",
        "knowledge_source_version",
        ["current_version_id"],
        ["version_id"],
    )


def downgrade() -> None:
    raise NotImplementedError("Rollback not implemented yet -- restore from a backup instead.")
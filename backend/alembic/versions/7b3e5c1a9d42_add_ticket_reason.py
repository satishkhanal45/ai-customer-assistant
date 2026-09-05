"""add ticket.reason

The Ticket Agent asks the customer why they want a ticket before collecting
their email, but the answer had nowhere to live and was discarded. This adds
the column it belongs in.

Nullable with no backfill: escalation paths open a ticket without asking, and
every row created before this migration genuinely has no reason recorded.

Revision ID: 7b3e5c1a9d42
Revises: 4c2d8a1f9e0b
Create Date: 2026-09-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "7b3e5c1a9d42"
down_revision: Union[str, None] = "4c2d8a1f9e0b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket", "reason")

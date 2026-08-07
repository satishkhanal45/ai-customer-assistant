"""create ticket table

Revision ID: 1e4beb1b8d68
Revises: 11160c9078cc
Create Date: 2026-08-06 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '1e4beb1b8d68'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('ticket',
    sa.Column('ticket_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('query', sa.Text(), nullable=False),
    sa.Column('priority', sa.String(length=32), nullable=True),
    sa.Column('status', sa.Enum('OPEN', 'IN_PROGRESS', 'RESOLVED', name='ticket_status'), server_default='OPEN', nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('ticket_id')
    )


def downgrade() -> None:
    op.drop_table('ticket')
    op.execute('DROP TYPE IF EXISTS ticket_status')
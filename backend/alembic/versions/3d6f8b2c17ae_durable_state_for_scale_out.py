"""Move two pieces of in-process state into the database (P2-5).

Both changes exist because the state they replace was held in a Python dict
on one process, which quietly made a documented guarantee false as soon as a
second instance existed -- or as soon as the single instance restarted.

1. ``ticket.idempotency_key`` + a unique index. Ticket deduplication lived in
   ``TicketStore._by_key``. Two API instances behind a load balancer each held
   their own empty dict, so a retried ticket creation booked a second row and
   sent the customer a second confirmation email. The index moves the
   guarantee to the one place both instances share.

   The column is nullable and the constraint is a plain UNIQUE, so unkeyed
   tickets (escalation paths with no thread to derive a key from) are
   unaffected: Postgres does not treat two NULLs as equal.

2. ``crawl_discovery``. The site-crawl review step cached its discovery list
   in a module-level dict in ``api/ingest.py``, so a discovery started on
   instance A could not be confirmed on instance B -- the confirm returned
   404 "unknown or expired discovery_id" when nothing had expired.

Both are additive. Nothing is dropped, no existing row changes, and the
downgrade is exact.

Revision ID: 3d6f8b2c17ae
Revises: 9f1a2b7c4e08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3d6f8b2c17ae"
down_revision: Union[str, None] = "9f1a2b7c4e08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("idempotency_key", sa.String(length=255), nullable=True))
    op.create_unique_constraint("uq_ticket_idempotency_key", "ticket", ["idempotency_key"])

    op.create_table(
        "crawl_discovery",
        sa.Column("discovery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
    )
    # Expired rows are swept opportunistically on lookup, which is a range
    # scan over expires_at on every discover/confirm.
    op.create_index("ix_crawl_discovery_expires_at", "crawl_discovery", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_crawl_discovery_expires_at", table_name="crawl_discovery")
    op.drop_table("crawl_discovery")
    op.drop_constraint("uq_ticket_idempotency_key", "ticket", type_="unique")
    op.drop_column("ticket", "idempotency_key")

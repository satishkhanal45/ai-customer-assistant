"""Seed the default service-account app_user row.

Uploads default ``uploaded_by`` to the nil UUID
(00000000-0000-0000-0000-000000000000), which is a foreign key to
``app_user.id``. Without a matching row, the ``knowledge_source`` insert
fails with a foreign-key violation. This data migration guarantees the row
exists on any schema upgrade, and is a no-op when it already does.

Revision ID: 4c2d8a1f9e0b
Revises: 2a1c9f0e45d7
Create Date: 2026-08-20
"""

from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "4c2d8a1f9e0b"
down_revision: Union[str, None] = "2a1c9f0e45d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    service_account = sa.table(
        "app_user",
        sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.column("email", sa.String(255)),
        sa.column("is_service_account", sa.Boolean()),
    )
    op.execute(
        sa.dialects.postgresql.insert(service_account)
        .values(
            id=sa.text("'00000000-0000-0000-0000-000000000000'"),
            email="system@alpinist.local",
            is_service_account=True,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM app_user "
            "WHERE id = '00000000-0000-0000-0000-000000000000'"
        )
    )
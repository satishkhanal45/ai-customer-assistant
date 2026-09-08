"""Identity, revocable sessions and durable rate-limit counters (P0-3).

Three changes, all additive. Nothing is dropped, no existing row's meaning
changes, and the downgrade is exact.

1. **Four columns on ``app_user``.** That table already existed as the
   target of ``knowledge_source.uploaded_by``, but it held only an email and
   a service-account flag -- there was nowhere to put a password, a role, or
   the fact that someone has left. ``password_hash`` is nullable because the
   seeded service account has none and never logs in.

   ``role`` is a string with a server default of ``'member'`` rather than an
   ``is_admin`` boolean. The two cost the same today; the string is what
   makes inserting a middle tier later a one-line change instead of a
   migration plus a rewrite of every call site.

   Backfill note: existing rows take the server defaults, so every account
   that predates this migration becomes an active ``member``. The service
   account is then promoted to ``admin`` below -- not because it logs in,
   but because ``uploaded_by`` points at it and a row that owns every
   existing document should not be the least privileged one in the table.

2. **``refresh_token``.** Refresh is rotating, so a token presented twice is
   either a race or a theft; this table is what makes that observable. It
   stores the ``jti`` claim, never the token, so a database leak does not
   hand over usable credentials.

3. **``rate_limit_bucket``.** One row per (key, fixed window). The composite
   primary key is the point: it turns the increment into a single atomic
   ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``, so two concurrent
   requests cannot both read 4 and both write 5. Holding these counters in a
   process dict would repeat the defect P2-5 fixed twice already -- N
   instances would multiply every limit by N, and a restart would forget
   that anyone had ever been throttled.

Revision ID: 8e5a3c9d21f7
Revises: 3d6f8b2c17ae
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8e5a3c9d21f7"
down_revision: Union[str, None] = "3d6f8b2c17ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SERVICE_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.add_column("app_user", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "app_user",
        sa.Column(
            "role", sa.String(length=16), nullable=False, server_default="member"
        ),
    )
    op.add_column(
        "app_user",
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "app_user", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True)
    )
    # A role outside the known set must never be readable as "some access" --
    # auth.roles raises on an unknown value, and this stops one being written.
    op.create_check_constraint(
        "ck_app_user_role", "app_user", sa.text("role IN ('member', 'admin')")
    )
    # A literal with an explicit cast rather than a bind parameter: psycopg
    # sends an untyped bind as VARCHAR, and Postgres has no uuid = varchar
    # operator, so the parameterised form fails outright.
    op.execute(
        sa.text(
            f"UPDATE app_user SET role = 'admin' "
            f"WHERE id = '{SERVICE_ACCOUNT_ID}'::uuid"
        )
    )

    op.create_table(
        "refresh_token",
        sa.Column("jti", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
    )
    # Revoking every token for one user is the response to a reuse, so that
    # lookup is on the hot path of the one operation that must not be slow.
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    op.create_index("ix_refresh_token_expires_at", "refresh_token", ["expires_at"])

    op.create_table(
        "rate_limit_bucket",
        sa.Column("key", sa.String(length=200), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_rate_limit_bucket_window_start", "rate_limit_bucket", ["window_start"]
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_bucket_window_start", table_name="rate_limit_bucket")
    op.drop_table("rate_limit_bucket")

    op.drop_index("ix_refresh_token_expires_at", table_name="refresh_token")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_table("refresh_token")

    op.drop_constraint("ck_app_user_role", "app_user", type_="check")
    op.drop_column("app_user", "last_login_at")
    op.drop_column("app_user", "is_active")
    op.drop_column("app_user", "role")
    op.drop_column("app_user", "password_hash")

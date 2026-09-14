"""Add the `visitor` role.

**Recreated 2026-09-14.** This file went missing from the working tree while
the database remained stamped at its revision, which left the API unable to
start: `Can't locate revision identified by 'f3c72a1d8b64'`. Alembic needs the
file to move in either direction, so it is restored here unchanged in effect
from the version that was applied. Nothing about the database is altered by
recreating it.

Original note:

Self-service signup opened the application to anyone, and `member` — which
grants uploading documents, running crawls and browsing the knowledge graph —
was what a stranger received. `visitor` sits *below* `member` in
`auth.roles._RANK`, so every existing `require_member` guard began excluding
visitors without being edited.

**Only the constraint changes. No row is touched.** This migration cannot tell
which existing members were self-service signups and which an admin created,
and silently demoting a colleague is worse than leaving one account too
privileged.

Revision ID: f3c72a1d8b64
Revises: e7b04d2c9a13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3c72a1d8b64"
down_revision: Union[str, None] = "e7b04d2c9a13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_app_user_role", "app_user", type_="check")
    op.create_check_constraint(
        "ck_app_user_role", "app_user", sa.text("role IN ('visitor', 'member', 'admin')")
    )


def downgrade() -> None:
    # Anything that could not exist before this ran has to go somewhere, and
    # `member` is the only other role a person can hold. Deliberately an
    # *upgrade* in privilege rather than a delete: dropping the accounts would
    # break `knowledge_source.uploaded_by`, and refusing to downgrade at all
    # would leave the constraint uncreatable.
    op.execute("UPDATE app_user SET role = 'member' WHERE role = 'visitor'")
    op.drop_constraint("ck_app_user_role", "app_user", type_="check")
    op.create_check_constraint(
        "ck_app_user_role", "app_user", sa.text("role IN ('member', 'admin')")
    )

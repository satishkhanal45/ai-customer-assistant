"""Add the `visitor` role.

Self-service signup opened the application to anyone, and `member` — which
grants uploading documents, running crawls and browsing the knowledge graph —
was what a stranger received. A prospective client asking what you charge and
a colleague curating the corpus are two different populations, and they should
not share a permission set merely because both are signed in.

`visitor` sits *below* `member` in `auth.roles._RANK`. Because `satisfies` is
a rank comparison, inserting a tier underneath made every existing
`require_member` guard start excluding visitors without being edited.

The alternative was renaming `member` to `visitor` and keeping two roles. It
would have been worse: the role travels inside the JWT, so a rename
invalidates every live session, and it needs this migration *plus* a rewrite
of every call site. Adding a tier below costs only the constraint.

**Only the constraint changes. No row is touched.** Existing `member`
accounts stay members — this migration cannot tell which of them were
self-service signups and which an admin created, and silently demoting a
colleague is worse than leaving one account too privileged. Downgrade those
by hand.

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

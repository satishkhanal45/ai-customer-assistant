"""Provider API keys, encrypted, with a default provider.

Until now a provider key came only from the environment. This table lets an
administrator rotate one from the Admin page without a redeploy, at the cost
of the key existing in the database at all -- which is why what is stored is
AES-GCM ciphertext under a key derived from `AUTH_SECRET`, never plaintext.

`last4` is stored in clear on purpose: the UI has to be able to say *which*
key is configured without the server ever returning the key, and four
characters cannot be used to call anything.

Seeds one row: groq, marked default, with no key. That records the intended
provider without asserting a credential exists -- resolution falls back to
`GROQ_API_KEY` in the environment exactly as before.

Revision ID: 9a4f7c2b83d1
Revises: 8e5a3c9d21f7
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a4f7c2b83d1"
down_revision: Union[str, None] = "8e5a3c9d21f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_credential",
        sa.Column("provider", sa.String(length=32), primary_key=True),
        sa.Column("encrypted_key", sa.Text(), nullable=True),
        sa.Column("last4", sa.String(length=8), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Exactly one default, enforced by the database rather than by whoever
    # remembers to clear the old one.
    op.create_index(
        "uq_llm_credential_single_default",
        "llm_credential",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.execute(
        sa.text(
            "INSERT INTO llm_credential (provider, is_default) VALUES ('groq', true) "
            "ON CONFLICT (provider) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_index("uq_llm_credential_single_default", table_name="llm_credential")
    op.drop_table("llm_credential")

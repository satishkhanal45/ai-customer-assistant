"""Retry bookkeeping and a dead-letter state for knowledge_injection_job.

Every ingestion failure used to be terminal. `complete_job` wrote FAILED and
that was the end of the document, so a job that hit a per-minute rate limit or
a Tika that happened to be restarting needed a human to notice and re-queue it
by hand. After the deterministic failures were fixed (ingestion.md items 8 and
8b), what remained in this database was almost entirely transient -- the last
two documents failed on a *daily* token ceiling, which a retry an hour later
simply walks past.

Three columns and one enum value:

* `attempt_count`  -- how many times the job has been tried.
* `next_attempt_at`-- holds a requeued job back until its backoff elapses.
                      NULL means "ready now", which is why every existing row
                      stays claimable without a backfill.
* `failure_kind`   -- the failing stage as a bare code, beside the
                      human-readable `error_details`. Grouping failures used
                      to require `split_part(error_details, ':', 1)` in every
                      caller (ingestion.md item 13).
* `DEAD_LETTER`    -- a job that exhausted its retries. Deliberately distinct
                      from FAILED: FAILED means "this cannot work as it
                      stands", DEAD_LETTER means "this kept failing for a
                      reason that usually passes, and we stopped trying".
                      They need different things from a human.

`failure_kind` is backfilled from the existing `error_details`, whose format
has always been "<kind>: <detail>" -- so the 51 failures already in this
database become groupable immediately rather than only newly-failing ones.

Revision ID: c4f8b2e17a90
Revises: b7d1e93a5c40
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4f8b2e17a90"
down_revision: Union[str, None] = "b7d1e93a5c40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres will not let a new enum value be *used* in the same transaction
    # that adds it. Alembic wraps each migration in one, so this runs in an
    # autocommit block -- otherwise the ALTER succeeds and any later statement
    # referencing DEAD_LETTER fails with "unsafe use of new value".
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE knowledge_job_status ADD VALUE IF NOT EXISTS 'DEAD_LETTER'"
        )

    op.add_column(
        "knowledge_injection_job",
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "knowledge_injection_job",
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "knowledge_injection_job",
        sa.Column("failure_kind", sa.Text(), nullable=True),
    )

    # Existing failures carry their kind in the text. `split_part` returns the
    # whole string when there is no colon, so guard on the colon rather than
    # writing a kind that is really a sentence.
    op.execute(
        """
        UPDATE knowledge_injection_job
           SET failure_kind = split_part(error_details, ':', 1)
         WHERE status = 'FAILED'
           AND error_details IS NOT NULL
           AND position(':' in error_details) > 0
        """
    )

    # Every already-failed job has been tried at least once; leaving these at
    # 0 would hand each of them a full fresh retry budget the first time they
    # are re-queued, which is not what the history says happened.
    op.execute(
        "UPDATE knowledge_injection_job SET attempt_count = 1 "
        "WHERE status = 'FAILED'"
    )

    # The claim query filters on (status, next_attempt_at) on every poll.
    op.create_index(
        "ix_job_claimable",
        "knowledge_injection_job",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_claimable", table_name="knowledge_injection_job")
    op.drop_column("knowledge_injection_job", "failure_kind")
    op.drop_column("knowledge_injection_job", "next_attempt_at")
    op.drop_column("knowledge_injection_job", "attempt_count")
    # The DEAD_LETTER enum value is deliberately left in place. Postgres has
    # no DROP VALUE, and removing it would mean recreating the type and
    # rewriting every row that references it -- far more destructive than the
    # unused label it would remove. Any row still holding DEAD_LETTER is
    # moved to FAILED so the column stays meaningful without it.
    op.execute(
        "UPDATE knowledge_injection_job SET status = 'FAILED' "
        "WHERE status = 'DEAD_LETTER'"
    )

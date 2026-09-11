"""Normalized uniqueness for `value` (ingestion.md: paraphrase duplicates).

`value` was unique on the exact text, so anything short of a byte-identical
repeat became a second fact. The corpus accumulated pairs that are not two
facts at all:

    Parbati B. / role        "PHP Intern"           "php intern"
    Waterfall / suitable_for "pre-defined projects" "pre‑defined projects"

The second pair differs by one character: U+002D HYPHEN-MINUS against U+2011
NON-BREAKING HYPHEN. Structured lookup returned both, as though the document
had stated two different roles.

This adds `value_norm` -- case-folded, whitespace-collapsed, with Unicode
punctuation variants mapped to ASCII -- and makes (entity, attribute,
value_norm) unique, so the duplicate becomes unrepresentable rather than
relying on every writer to normalize. The old exact-text constraint is left in
place; it is implied by the new one and dropping it buys nothing.

**Order matters.** Existing duplicates are merged before the constraint is
added, or creating it fails against exactly the data it exists to prevent --
the same shape as `b7d1e93a5c40` for chunks.

**Provenance is repointed, not cascaded away.** `value_provenance.value_id`
is ON DELETE CASCADE, so deleting a duplicate would silently drop the record
that some version asserted that fact. The survivor would then look unasserted
and `resolve_superseded_values` would mark it superseded -- the merge would
have deleted a current fact by a side effect two tables away. So every
loser's provenance is moved onto the survivor first.

**Which row survives:** the longest text, then the one with the most capital
letters, then the lowest id. Length first because the longer form is usually
the more complete telling; capitals as the tie-break because between
"PHP Intern" and "php intern" the former is the better thing to show someone.

Revision ID: e7b04d2c9a13
Revises: d5a91c3f7b28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b04d2c9a13"
down_revision: Union[str, None] = "d5a91c3f7b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize(value: str) -> str:
    """A copy of `ingestion.values.normalize_value`.

    Copied deliberately. A migration describes what the database did on the
    day it ran, and importing application code would let a later edit to that
    function silently change the meaning of this one.
    """
    import re
    import unicodedata

    folded = unicodedata.normalize("NFKC", value)
    folded = re.sub(r"[‐-―−]", "-", folded)
    folded = re.sub(r"[‘’‛′]", "'", folded)
    folded = re.sub(r"[“”″]", '"', folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded.casefold()


def _survivor(rows: list) -> str:
    """Pick the row to keep from one duplicate group."""
    return max(
        rows,
        key=lambda r: (
            len(r.value),
            sum(1 for ch in r.value if ch.isupper()),
            # Lowest id wins the final tie, so the choice is deterministic.
            str(r.id),
        ),
    ).id


def upgrade() -> None:
    op.add_column(
        "value",
        sa.Column("value_norm", sa.Text(), nullable=False, server_default=""),
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, entity_id, attribute_id, value FROM value")
    ).fetchall()

    groups: dict[tuple, list] = {}
    for row in rows:
        connection.execute(
            sa.text("UPDATE value SET value_norm = :n WHERE id = :i"),
            {"n": _normalize(row.value), "i": row.id},
        )
        key = (row.entity_id, row.attribute_id, _normalize(row.value))
        groups.setdefault(key, []).append(row)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = _survivor(group)
        losers = [r.id for r in group if r.id != keeper]

        # Move provenance across before the delete cascades it away.
        connection.execute(
            sa.text(
                """
                INSERT INTO value_provenance (value_id, version_id)
                SELECT :keeper, version_id FROM value_provenance
                 WHERE value_id = ANY(:losers)
                ON CONFLICT DO NOTHING
                """
            ),
            {"keeper": keeper, "losers": losers},
        )
        connection.execute(
            sa.text("DELETE FROM value WHERE id = ANY(:losers)"), {"losers": losers}
        )
        merged += len(losers)

    print(f"value normalization: merged {merged} duplicate row(s)")

    op.create_unique_constraint(
        "uq_value_normalized", "value", ["entity_id", "attribute_id", "value_norm"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_value_normalized", "value", type_="unique")
    op.drop_column("value", "value_norm")

"""The role ordering.

Two roles, and the line between them is **"uses the system" vs. "changes the
system"**:

* ``member`` — everything the application currently does: ask questions,
  browse the knowledge graph, upload documents, run crawls, watch jobs.
  Anyone with an account is a trusted colleague who can add content.
* ``admin`` — all of that, plus the system's own controls: who has accounts,
  what the prompts and retrieval parameters are, and destroying sources.

Across the endpoints that exist today the two are almost identical; the only
live difference is account creation. What the split buys is that the
*unbuilt* administrative endpoints — prompt management, retrieval
configuration, source deletion — have somewhere to land that is not the same
gate as uploading a PDF. Those are the operations where a mistake is silent:
change ``similarity_threshold`` and every answer degrades with no error
anywhere, whereas a member's worst case is a bad document, which is visible
and deletable.

The role is stored as an ordered string, never as an ``is_admin`` boolean.
The two cost the same today, but inserting a middle tier later costs one
entry in ``_RANK`` and a reclassification of a few endpoints, rather than a
migration plus a rewrite of every call site.
"""

from __future__ import annotations

from typing import Final

MEMBER: Final[str] = "member"
ADMIN: Final[str] = "admin"

DEFAULT_ROLE: Final[str] = MEMBER

# Ascending authority. The values are the ranks; only their order matters.
_RANK: Final[dict[str, int]] = {MEMBER: 10, ADMIN: 20}

ALL_ROLES: Final[tuple[str, ...]] = tuple(
    sorted(_RANK, key=lambda role: _RANK[role])
)


class UnknownRoleError(ValueError):
    """A role string that is not in the ordering.

    Raised rather than defaulted: a typo in a role name must not silently
    grant or deny access, and an unrecognised value in the database is a
    fact worth crashing over.
    """


def rank(role: str) -> int:
    try:
        return _RANK[role]
    except KeyError:
        raise UnknownRoleError(
            f"Unknown role {role!r}. Known roles: {', '.join(ALL_ROLES)}."
        ) from None


def satisfies(actual: str, minimum: str) -> bool:
    """Whether ``actual`` meets or exceeds ``minimum``.

    A comparison rather than a set membership test, so ``admin`` passes every
    ``member`` check without anyone having to remember to list it. Roles that
    cannot be composed wrongly are roles nobody misconfigures.
    """
    return rank(actual) >= rank(minimum)

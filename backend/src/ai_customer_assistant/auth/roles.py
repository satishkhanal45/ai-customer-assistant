"""The role ordering.

Three roles, and the line between them is **who the person is to the
company**:

* ``visitor`` -- anyone. Chat is public, so nothing creates these accounts
  today; the tier remains the floor of the ordering, and an account that
  still holds it can sign in and reach the assistant and nothing else.
* ``member`` -- a colleague, created by an admin rather than self-service.
  Adds content: upload documents, run crawls, watch jobs, browse the
  knowledge graph.
* ``admin`` -- all of that, plus the system's own controls: who has accounts,
  the agent prompts, the provider API keys, and destroying sources.

**Why three rather than two.** ``visitor`` and ``member`` are two genuinely
different populations: a stranger and a colleague should not share a
permission set merely because both are signed in. The alternative considered
was renaming ``member`` to ``visitor`` and keeping two roles, which would have
been strictly worse -- the role travels inside the JWT, so renaming it
invalidates every live session, and it needs a data migration and a rewrite of
every call site. Adding a tier *below* the existing one costs neither.

The ordering is what makes that cheap. ``satisfies`` is a rank comparison, so
inserting ``visitor`` below ``member`` made every existing ``require_member``
guard start excluding visitors **without being edited** -- ingestion, crawling
and the graph API were all correct the moment the rank existed.

The role is stored as an ordered string, never as an ``is_admin`` boolean.
The ranks are spaced by five so a further tier can be inserted between any
two without renumbering the others.
"""

from __future__ import annotations

from typing import Final

VISITOR: Final[str] = "visitor"
MEMBER: Final[str] = "member"
ADMIN: Final[str] = "admin"

#: What an *admin* creating an account gets if they do not say otherwise.
DEFAULT_ROLE: Final[str] = MEMBER

# Ascending authority. The values are the ranks; only their order matters.
_RANK: Final[dict[str, int]] = {VISITOR: 5, MEMBER: 10, ADMIN: 20}

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

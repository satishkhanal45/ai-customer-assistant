"""What counts as "in domain", derived from what is actually ingested (F6).

## The defect

`prompt.DOMAIN_DEFINITION` is a hand-written paragraph describing Alpinist
Studios' services. The classifier decides scope from it alone, and it has no
connection whatsoever to the knowledge base. So the assistant confidently
refuses questions it can answer, whenever the topic is real but absent from
that paragraph.

Observed live, and reproduced deliberately:

    "Why did Soani Tech change its name?"
        -> OUT_OF_SCOPE, domain_confidence 0.95
        -> "I'm sorry, I am only able to help with solving the problem you
            are facing on our platform."

while the corpus holds a whole document called
`soani-tech-is-now-alpinist-studios` about exactly that.

Note what this is *not*. It is tempting to treat a wrong refusal as
low-confidence and route uncertain classifications through retrieval — that
was the first fix proposed, and measuring it killed it:

    "Why did Soani Tech change its name?"  -> OUT_OF_SCOPE  0.95
    "How's the weather today?"             -> OUT_OF_SCOPE  0.98

The classifier is not hesitant. It is *sure*, and wrong, because nobody ever
told it what the company has published. No confidence threshold separates
those two.

## The fix

Append the titles of the live documents to the domain definition. Measured
with a single classify call per question, before and after:

    question                              static          corpus-aware
    Why did Soani Tech change its name?   OUT_OF_SCOPE    DOMAIN_REQUEST
    Are you hiring AI engineers?          DOMAIN_REQUEST  DOMAIN_REQUEST
    How's the weather today?              OUT_OF_SCOPE    OUT_OF_SCOPE

The scope widens to cover the corpus without softening the genuine
out-of-scope guard, which is the property that matters: an assistant that
accepts everything is not an improvement on one that refuses too much.

## Why it refreshes rather than being read once

The worker ingests continuously. A definition captured at startup would go
stale the moment a document landed, and the symptom — refusing a question
about a document you just added — is exactly the bug this fixes. So the
titles are re-read on a TTL, from the chat path, and every failure degrades
to the last known good value (ultimately the static paragraph). The
classifier must keep working when Postgres does not.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from .prompt import DOMAIN_DEFINITION

logger = logging.getLogger(__name__)

# How long a snapshot of the corpus is trusted. Short enough that a freshly
# ingested document becomes answerable within minutes; long enough that the
# query is negligible next to a chat turn.
DEFAULT_TTL_SECONDS = 300.0

# Titles are cheap but not free — they ride along in every classification
# prompt. Enough to characterise the corpus, not enough to crowd the prompt.
DEFAULT_MAX_SOURCES = 40


def live_sources_statement(limit: int = DEFAULT_MAX_SOURCES):
    """Pure: the titles a question may legitimately be about.

    Scoped **exactly** like `vector_search`'s join contract — active source,
    current version, status INDEXED. Matching only the first two conditions
    was wrong in a way the live log caught: 21 sources qualified where
    retrieval would only ever return 9, so the classifier could accept a
    question about a document that vector search is contractually forbidden
    to surface. That trades a scope refusal for a groundedness refusal, which
    is better but still a refusal.
    """
    from sqlalchemy import select

    from agents.knowledge.constants import VERSION_STATUS_INDEXED
    from db.models import KnowledgeSource, KnowledgeSourceVersion

    return (
        select(KnowledgeSource.source_name)
        .join(
            KnowledgeSourceVersion,
            KnowledgeSourceVersion.version_id == KnowledgeSource.current_version_id,
        )
        .where(
            KnowledgeSource.is_active.is_(True),
            KnowledgeSourceVersion.status == VERSION_STATUS_INDEXED,
        )
        .order_by(KnowledgeSource.updated_at.desc())
        .limit(limit)
    )


def render_definition(base: str, source_names: Sequence[str]) -> str:
    """Pure: the domain definition, widened by what the corpus holds.

    Source names are used verbatim — slugs, filenames and URLs alike. That is
    what the A/B above measured, and prettifying them is an untested change to
    a prompt, which is how the uncalibrated retrieval threshold got there.
    """
    if not source_names:
        return base
    return (
        f"{base} The knowledge base currently holds documents titled: "
        f"{', '.join(source_names)}. A question that plausibly concerns any "
        f"of these documents is IN DOMAIN."
    )


class CorpusScope:
    """The domain definition, kept roughly in step with the knowledge base.

    `current()` never blocks and never raises — it is called from the
    synchronous classification node on every turn. `refresh_if_stale()` is
    the async half, driven from the chat path, and it is the only thing that
    touches the database.
    """

    def __init__(
        self,
        base: str = DOMAIN_DEFINITION,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_sources: int = DEFAULT_MAX_SOURCES,
    ) -> None:
        self._base = base
        self._ttl_seconds = ttl_seconds
        self._max_sources = max_sources
        self._definition = base
        self._expires_at = 0.0

    def current(self) -> str:
        """The definition to classify against right now."""
        return self._definition

    def is_stale(self) -> bool:
        return time.monotonic() >= self._expires_at

    async def refresh_if_stale(self, session_factory) -> None:
        """Re-read the corpus titles if the TTL has elapsed.

        Failure is not propagated: a classifier that stops working because
        the knowledge base is briefly unreachable would be a worse bug than
        the one being fixed. The previous definition (or the static base)
        stays in force, and the TTL is pushed out so a database that is down
        is not queried once per turn.
        """
        if not self.is_stale():
            return
        self._expires_at = time.monotonic() + self._ttl_seconds
        try:
            names = await self._live_source_names(session_factory)
        except Exception:  # noqa: BLE001 - scope must survive a database blip
            logger.warning(
                "could not read the corpus for the domain definition; "
                "keeping the previous one",
                exc_info=True,
            )
            return
        self._definition = render_definition(self._base, names)
        logger.info("domain definition refreshed from %d live sources", len(names))

    async def _live_source_names(self, session_factory) -> list[str]:
        """The titles a customer could actually be asking about."""
        async with session_factory() as session:
            rows = await session.execute(live_sources_statement(self._max_sources))
        return [name for (name,) in rows.all() if name]


def static_scope(definition: Optional[str] = None) -> CorpusScope:
    """A scope that never queries anything — the default for tests and for a
    process with no database wired in."""
    scope = CorpusScope(definition or DOMAIN_DEFINITION)
    scope._expires_at = float("inf")
    return scope

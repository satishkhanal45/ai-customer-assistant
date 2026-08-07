"""
hybrid.py — retrieval strategy decision + fan-out/fan-in.

Two public functions with distinct jobs:

`decide_strategy(query, *, config)` is a pure function returning one of
"structured" / "vector" / "hybrid". This is what backs graph.py's
conditional edge — LangGraph calls it to pick which node(s) to route
state through next. It never touches a database or an LLM.

`hybrid_retrieve(...)` is a convenience orchestrator that runs
`decide_strategy` internally and then actually performs the retrieval:
a single call for the structured/vector strategies, a concurrent
fan-out for the hybrid strategy. graph.py's node wiring may call
`structured_lookup()` / `vector_search()` directly for the pure
single-strategy branches (see nodes.py) — `hybrid_retrieve()` exists so
the hybrid branch's fan-out/fan-in logic lives in exactly one place,
and so this module is fully testable and directly callable without a
running graph.

Graceful degradation, scoped precisely: the *hybrid* strategy is the
only place this module treats an arm's expected "found nothing" outcome
as a value rather than a failure — EntityNotFoundError /
AmbiguousEntityError / AttributeNotFoundError from the structured arm,
and EmptyRetrievalError from the vector arm, all degrade to an empty
tuple for that arm rather than aborting the whole hybrid_retrieve call,
because hybrid mode explicitly exists to combine two independent
signals and either one alone can still produce a useful RankedResult.
Any *other* exception from either arm (a genuine infrastructure
failure, not "no results") still propagates. The pure single-strategy
branches (structured-only, vector-only) have no second arm to fall back
on, so their exceptions always propagate — there's nothing to degrade
to.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from .config import KnowledgeAgentConfig
from .constants import STRATEGY_HYBRID, STRATEGY_STRUCTURED, STRATEGY_VECTOR
from .exceptions import AmbiguousEntityError, AttributeNotFoundError, EmptyRetrievalError, EntityNotFoundError
from .extraction import is_confident
from .structured_lookup import structured_lookup
from .types import RankedResult, RetrievedChunk, RewrittenQuery, StructuredFact, StructuredQuery
from .vector_search import EmbeddingFunction, vector_search

StructuredLookupFn = Callable[..., Awaitable[tuple[StructuredFact, ...]]]
VectorSearchFn = Callable[..., Awaitable[tuple[RetrievedChunk, ...]]]

GRACEFUL_STRUCTURED_MISSES: tuple[type[BaseException], ...] = (
    EntityNotFoundError,
    AmbiguousEntityError,
    AttributeNotFoundError,
)
GRACEFUL_VECTOR_MISSES: tuple[type[BaseException], ...] = (EmptyRetrievalError,)


# ==========================================================================
# Public API
# ==========================================================================


def decide_strategy(query: StructuredQuery, *, config: KnowledgeAgentConfig) -> str:
    """Pure decision function backing graph.py's conditional edge.

    - structured: an entity was resolved, a specific attribute or
      relation was requested, and extraction confidence clears the
      configured threshold — an exact-fact lookup.
    - hybrid: an entity was resolved and (a specific slot was
      requested OR confidence clears the threshold) but the structured
      case above didn't fully apply — a broad question about a named
      entity ("tell me everything about X"), or a specific-seeming
      request extraction wasn't fully confident about, benefits from
      structured facts as an anchor plus supporting documentation.
    - vector: no entity was resolved, or what was resolved carries too
      little confidence to anchor a lookup on — open-ended,
      explanatory, or procedural questions.

    Routing quality follows directly from extraction.py's confidence
    calibration (see prompts/extraction.md) — this function only
    combines already-extracted signals, it doesn't re-interpret the
    original query text."""
    has_entity = query.entity_type is not None
    has_specific_slot = query.attribute is not None or query.relation_type is not None
    confident = is_confident(query, config=config)

    return next(
        strategy
        for strategy, is_applicable in (
            (STRATEGY_STRUCTURED, has_entity and has_specific_slot and confident),
            (STRATEGY_HYBRID, has_entity and (has_specific_slot or confident)),
            (STRATEGY_VECTOR, True),
        )
        if is_applicable
    )


async def hybrid_retrieve(
    query: StructuredQuery,
    rewritten: RewrittenQuery,
    *,
    config: KnowledgeAgentConfig,
    session: AsyncSession,
    embed_query: EmbeddingFunction,
    structured_lookup_fn: StructuredLookupFn = structured_lookup,
    vector_search_fn: VectorSearchFn = vector_search,
) -> RankedResult:
    """Decide a strategy, then perform retrieval accordingly. The two
    retrieval functions are injectable (defaulting to the real
    structured_lookup / vector_search) so this orchestration logic is
    testable with simple async fakes, with zero database or embedding
    model required."""
    strategy = decide_strategy(query, config=config)
    handler = _STRATEGY_HANDLERS[strategy]
    return await handler(
        query,
        rewritten,
        config=config,
        session=session,
        embed_query=embed_query,
        structured_lookup_fn=structured_lookup_fn,
        vector_search_fn=vector_search_fn,
    )


# ==========================================================================
# Internals — per-strategy handlers (uniform signature for dispatch)
# ==========================================================================


async def _structured_only(
    query: StructuredQuery,
    rewritten: RewrittenQuery,
    *,
    config: KnowledgeAgentConfig,
    session: AsyncSession,
    embed_query: EmbeddingFunction,
    structured_lookup_fn: StructuredLookupFn,
    vector_search_fn: VectorSearchFn,
) -> RankedResult:
    facts = await structured_lookup_fn(query, session=session)
    return RankedResult(structured_facts=facts, retrieved_chunks=())


async def _vector_only(
    query: StructuredQuery,
    rewritten: RewrittenQuery,
    *,
    config: KnowledgeAgentConfig,
    session: AsyncSession,
    embed_query: EmbeddingFunction,
    structured_lookup_fn: StructuredLookupFn,
    vector_search_fn: VectorSearchFn,
) -> RankedResult:
    chunks = await vector_search_fn(rewritten, config=config, session=session, embed_query=embed_query)
    return RankedResult(structured_facts=(), retrieved_chunks=chunks)


async def _hybrid_fan_out(
    query: StructuredQuery,
    rewritten: RewrittenQuery,
    *,
    config: KnowledgeAgentConfig,
    session: AsyncSession,
    embed_query: EmbeddingFunction,
    structured_lookup_fn: StructuredLookupFn,
    vector_search_fn: VectorSearchFn,
) -> RankedResult:
    structured_outcome, vector_outcome = await asyncio.gather(
        structured_lookup_fn(query, session=session),
        vector_search_fn(rewritten, config=config, session=session, embed_query=embed_query),
        return_exceptions=True,
    )
    return RankedResult(
        structured_facts=_resolve_arm(structured_outcome, graceful_types=GRACEFUL_STRUCTURED_MISSES),
        retrieved_chunks=_resolve_arm(vector_outcome, graceful_types=GRACEFUL_VECTOR_MISSES),
    )


_STRATEGY_HANDLERS: Mapping[str, Callable[..., Awaitable[RankedResult]]] = {
    STRATEGY_STRUCTURED: _structured_only,
    STRATEGY_VECTOR: _vector_only,
    STRATEGY_HYBRID: _hybrid_fan_out,
}


def _resolve_arm(outcome: object, *, graceful_types: tuple[type[BaseException], ...]) -> tuple:
    """An expected 'found nothing' exception from one arm of a hybrid
    fan-out becomes an empty tuple; anything else re-raises, since only
    the specific exceptions in `graceful_types` represent a legitimate
    empty result rather than an actual failure."""
    if isinstance(outcome, BaseException):
        if isinstance(outcome, graceful_types):
            return ()
        raise outcome
    return outcome
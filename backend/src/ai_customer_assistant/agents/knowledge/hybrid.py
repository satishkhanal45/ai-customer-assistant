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

Graceful degradation, scoped precisely: an arm's expected "found
nothing" outcome is treated as a value rather than a failure —
EntityNotFoundError / AmbiguousEntityError / AttributeNotFoundError
from the structured arm, and EmptyRetrievalError from the vector arm,
all degrade to an empty tuple for that arm rather than aborting the
whole call. Any *other* exception (a genuine infrastructure failure,
not "no results") still propagates.

## The structured-only fallback (P1-6)

A structured-only retrieval that finds nothing now falls back to vector
search instead of returning an empty result.

This is not a nicety. `decide_strategy` routes to structured-only
whenever the extractor named an entity type, asked for a specific slot,
and *was confident*. When that lookup then finds nothing, the old code
returned an empty RankedResult and the agent told the customer it had
no information — while the semantic index held the answer the whole
time. Observed live:

    query:       "Does the company support remote or hybrid work?"
    extraction:  entity_type='Policy', relation_type='supports', 0.85
    strategy:    structured   (0.85 clears the confidence threshold)
    structured:  0 facts      ('Policy' is a real ontology type, but no
                               Policy entities exist in this graph)
    vector:      never ran    -- it would have returned the answering
                               chunk at similarity 0.519
    answer:      "I'm sorry, I don't have information on ..."

Note what is *not* wrong there: the extraction is defensible and the
entity type is legitimate. The failure is structural — a confident
extraction is allowed to switch off the retrieval path that works, and
the more certain the extractor sounds the more often it happens. So
the fix belongs here, not in the extractor: whatever the reason a
structured lookup comes up empty (unknown entity, no instances, missing
attribute, ambiguity), falling back to semantic search costs one query
and can only add information.

The fallback deliberately does **not** apply to the hybrid strategy —
vector search has already run there — nor to a structured lookup that
did find facts.

`should_fall_back_to_vector` is the single definition of that rule.
Both entry points call it: `hybrid_retrieve` here, and the conditional
edge `nodes.make_structured_fallback_edge` that the compiled graph
actually traverses. They are two doors into the same behaviour, and
this module exists to keep exactly that kind of logic in one place.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from .config import KnowledgeAgentConfig
from .constants import STRATEGY_HYBRID, STRATEGY_STRUCTURED, STRATEGY_VECTOR
from .exceptions import AmbiguousEntityError, AttributeNotFoundError, EmptyRetrievalError, EntityNotFoundError
from .fact_relevance import relevant_facts
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

    - hybrid: an entity was resolved. Structured facts anchor the answer,
      documentation supports it, and the two arms run concurrently.
    - vector: no entity was resolved — open-ended, explanatory, or
      procedural questions with nothing to look up.

    ## Why there is no longer a structured-*only* strategy (F5)

    There was a third outcome: an entity, a specific slot, and confidence
    above the threshold meant an exact-fact lookup with **no semantic search
    at all**. That made the answer depend on the exact shape of a
    non-deterministic extraction. Observed live, same question, same
    `temperature=0`:

        run A:  entity_type='Policy',  relation_type='supports', 0.85
                -> slot + confident   -> structured-only, no chunks
        run B:  entity_type='Company', no slot,                  0.62
                -> confident, no slot -> hybrid, with chunks

    Two different retrieval plans, two different contexts, two different
    answers to one question. No amount of tuning the confidence threshold
    fixes that, because the runs did not differ in confidence — they
    differed in *shape*, and any router keyed on shape inherits the
    extractor's variance.

    So the extraction is treated as a hint about what to *add*, never as a
    switch that turns retrieval off. Whenever an entity is worth looking up,
    semantic search runs alongside it. It costs nothing worth counting:
    measured at 0.14-0.24s, and `_hybrid_fan_out` runs both arms
    concurrently, so the wall-clock cost is usually zero.

    The confidence score is not consulted either, for the same reason in a
    second guise: two runs landing either side of the threshold (0.54 and
    0.56) would otherwise choose different plans, which is the same defect
    with a different trigger. Routing now asks one question — *is there an
    entity to look up?* — and nothing about how sure the extractor felt.

    That is safe because the structured arm is guarded downstream rather
    than upstream: an unresolvable entity degrades to no facts
    (`GRACEFUL_STRUCTURED_MISSES`), and an unslotted dump is filtered
    against the question by `fact_relevance` (F4). Confidence was a poor
    proxy for both, and it cost determinism to consult.

    `STRATEGY_STRUCTURED` is deliberately still implemented — see
    `should_fall_back_to_vector` and `_structured_only`. It is now a
    backstop rather than a routing outcome: `hybrid_retrieve` is a public
    function that a caller could drive with any strategy, and deleting the
    P1-6 fallback would mean re-introducing that bug the moment anything
    routes there again. `test_f5_routing_consistency.py` asserts this
    function never selects it.

    Routing quality otherwise follows from extraction.py's confidence
    calibration (see prompts/extraction.md) — this function only combines
    already-extracted signals, it doesn't re-interpret the original query
    text."""
    has_entity = query.entity_type is not None

    return next(
        strategy
        for strategy, is_applicable in (
            (STRATEGY_HYBRID, has_entity),
            (STRATEGY_VECTOR, True),
        )
        if is_applicable
    )


def should_fall_back_to_vector(strategy: str, structured_facts: Sequence[StructuredFact]) -> bool:
    """Pure: should an empty structured result be retried semantically?

    True only for the structured-*only* strategy with nothing found. The
    hybrid strategy has already run vector search concurrently, so falling
    back there would re-run it; and a lookup that found facts has nothing
    to fall back from.

    Shared by `hybrid_retrieve` and by the compiled graph's conditional edge
    so the two cannot drift — see the module docstring.
    """
    return strategy == STRATEGY_STRUCTURED and not structured_facts


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
    """Exact-fact lookup, falling back to semantic search when it finds
    nothing (P1-6 — see the module docstring)."""
    try:
        facts = await structured_lookup_fn(query, session=session)
    except GRACEFUL_STRUCTURED_MISSES:
        # "No such entity/attribute" is the commonest way this strategy comes
        # up empty, and it is exactly the case the fallback exists for. It is
        # caught rather than propagated so both entry points behave alike:
        # the graph's structured_lookup node already degrades these to ().
        facts = ()
    facts = relevant_facts(facts, query=query, query_text=rewritten.rewritten_text)

    if not should_fall_back_to_vector(STRATEGY_STRUCTURED, facts):
        return RankedResult(structured_facts=facts, retrieved_chunks=())

    try:
        chunks = await vector_search_fn(
            rewritten, config=config, session=session, embed_query=embed_query
        )
    except GRACEFUL_VECTOR_MISSES:
        # Both paths empty: the corpus really has nothing. That is a
        # legitimate answer, not an error.
        chunks = ()
    return RankedResult(structured_facts=facts, retrieved_chunks=chunks)


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
        structured_facts=relevant_facts(
            _resolve_arm(structured_outcome, graceful_types=GRACEFUL_STRUCTURED_MISSES),
            query=query,
            query_text=rewritten.rewritten_text,
        ),
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
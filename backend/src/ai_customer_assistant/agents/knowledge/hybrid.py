"""
hybrid.py — the retrieval strategy decision, as pure policy.

Two things live here, and nothing else:

`decide_strategy(query, *, config)` returns "hybrid" or "vector". It backs
graph.py's conditional fan-out — LangGraph calls it to pick which retrieval
node(s) a turn is routed through. It never touches a database or an LLM.

`should_fall_back_to_vector(strategy, facts)` decides whether an empty
structured result should be retried semantically. It backs the graph's
`structured_fallback` edge.

Both are pure functions over already-extracted signals. The retrieval itself
— opening sessions, running the two arms, handling their expected misses —
belongs to `nodes.py` and the graph topology in `graph.py`.

## What used to be here, and why it is gone

This module also carried `hybrid_retrieve()`, a "convenience orchestrator"
that ran the same fan-out directly, without a graph. It had no production
callers: the compiled graph reaches `structured_lookup` and `vector_search`
through its own nodes and has never imported it.

So it was a second implementation of four behaviours the graph already had —
strategy dispatch, graceful "found nothing" handling, the P1-6 vector
fallback and the F4 fact filter — kept in step by hand. Both P1-6 and F4
required matching edits in both copies, which is the drift the shared
ontology work (P2-1) was about, one layer down.

It was also quietly broken for most of its life: it took a single
`AsyncSession` while running both arms under `asyncio.gather`, which
SQLAlchemy refuses, so the one strategy it was named after raised. That was
fixed (F8) and then this removal made the fix moot — worth recording, because
"make the duplicate work" and "remove the duplicate" were both on the table
and only the second one shrinks the surface.

**Deleting it changed nothing about how the system retrieves.** Hybrid
retrieval is a property of the graph's fan-out edge, not of any function
named after it. Every query that resolves an entity still runs both arms
concurrently.

## Graceful degradation

An arm's expected "found nothing" outcome is a value, not a failure:
EntityNotFoundError / AmbiguousEntityError / AttributeNotFoundError from the
structured arm, and EmptyRetrievalError from the vector arm, degrade to an
empty tuple. Any *other* exception is a real fault and propagates. The
constants below name those two sets; `nodes.py` applies them.
"""

from __future__ import annotations

from typing import Sequence

from .config import KnowledgeAgentConfig
from .constants import STRATEGY_HYBRID, STRATEGY_STRUCTURED, STRATEGY_VECTOR
from .exceptions import AmbiguousEntityError, AttributeNotFoundError, EmptyRetrievalError, EntityNotFoundError
from .types import StructuredFact, StructuredQuery

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

    `STRATEGY_STRUCTURED` is never returned, but the constant and the
    `should_fall_back_to_vector` rule are kept: the graph's fallback edge
    still reads them, and deleting the rule would mean re-introducing P1-6
    the moment anything routes there again.
    `test_routing_consistency.py` asserts this function never selects it.

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

    Backs the compiled graph's `structured_fallback` edge.
    """
    return strategy == STRATEGY_STRUCTURED and not structured_facts

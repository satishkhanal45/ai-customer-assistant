"""
nodes.py — thin LangGraph node adapters.

Every node here does exactly one thing: unpack the fields it needs from
`KnowledgeAgentState`, call exactly one pure/async function from one of
the stage modules, and return a **partial update dict** containing only
the field(s) it changed — e.g. `{"rewritten_query": rewritten}`, never
a full state copy. No business logic lives here — see rewriting.py,
extraction.py, structured_lookup.py, vector_search.py, ranking.py,
deduplication.py, context_builder.py, prompt_builder.py, and llm.py
for the actual logic.

Why a partial dict and not `dataclasses.replace(state, field=value)`:
LangGraph applies every node's return value to its own per-field
channel, and by default each channel accepts only one write per step.
Verified empirically: when the hybrid strategy fans out to
`structured_lookup` and `vector_search` in parallel and *both* nodes
return a full state object (via `dataclasses.replace`), LangGraph
raises `InvalidUpdateError` — even for fields neither node changed,
because a full-object return still counts as a write to every field on
it. Returning only the changed field(s) as a dict means the two
parallel branches write to disjoint channels (`structured_facts` vs
`retrieved_chunks`) and merge cleanly. This is why every node function
below returns `{"field_name": value}` rather than a `KnowledgeAgentState`
instance, and why `Node`'s return type is `Awaitable[dict]`, not
`Awaitable[KnowledgeAgentState]`.

Dependency injection pattern: each node needs collaborators (config,
an LLM callable, a DB session, an embedding function) that a bare
`Callable[[KnowledgeAgentState], KnowledgeAgentState]` — LangGraph's
node signature — has no room to accept as extra arguments. So every
node here is built by a small factory function (`make_*_node`) that
closes over its dependencies at graph-construction time and returns
the actual node callable. `graph.py` calls these factories once, with
real dependencies, when it builds the graph; tests call them with
fakes, exactly like every other module in this package — no LangGraph
runtime required to test a single node in isolation.

Session injection: `structured_lookup_node` and `vector_search_node`
each receive a `session_factory` (an `async_sessionmaker`), not a
shared `AsyncSession` — every call opens and closes its own session
scoped to that single call. This matters specifically because the
hybrid strategy runs both nodes concurrently: a single `AsyncSession`
instance is not safe for concurrent use by two coroutines (verified —
sharing one session across the parallel fan-out raises
`sqlalchemy.exc.InvalidRequestError: This session is provisioning a
new connection; concurrent operations are not permitted`, in SQLite
and in Postgres alike). A session-per-call factory sidesteps this
entirely while still letting the two branches run genuinely in
parallel.

Blocking work inside async nodes: the three LLM nodes (`rewrite`,
`extract`, `llm`) call injected provider callables that are synchronous
and blocking -- the Groq SDK and `requests` under the hood, including
`time.sleep` backoff on a 429. Those calls are wrapped in
`asyncio.to_thread`.

This matters precisely because these nodes are `async def`. LangGraph
runs a *sync* node function in a thread executor, so blocking inside one
is harmless; an *async* node runs directly on the event loop, so blocking
inside one freezes the whole process -- every other user's request, the
health check, and any concurrently running ingestion job -- for the full
duration of the call. Three sequential LLM calls per Knowledge turn made
that a multi-second freeze on every question, and minutes long whenever
Groq rate-limited. `ingestion/pipeline.py` already used `to_thread` for
exactly this reason; these nodes now match it.

The pure stage functions stay synchronous and unchanged: the concurrency
concern belongs at the graph boundary, not inside `rewriting.py` or
`llm.py`, which remain trivially testable without an event loop.

Graceful-miss handling at the node level: `structured_lookup_node` and
`vector_search_node` both catch their respective "expected miss"
exceptions (`hybrid.GRACEFUL_STRUCTURED_MISSES` /
`hybrid.GRACEFUL_VECTOR_MISSES`, which name the two sets) and degrade
to an empty tuple in state, regardless of which strategy routed to
them. That is a deliberate graph-level choice: a confidently
determined "not found" is normal data flow — it feeds
context_builder.py's already-covered empty-section placeholders and
ultimately lets the LLM honestly say "I don't have enough
information" — not a reason to abort the customer's turn. A genuine
infrastructure failure (anything outside those two exception tuples)
still propagates out of the node, for graph.py / the Supervisor Agent
to handle as a real error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import KnowledgeAgentConfig
from .context_builder import build_context
from .deduplication import deduplicate
from .extraction import extract_query
from .extraction import LLMCompletion as ExtractionLLMCompletion
from .fact_relevance import relevant_facts
from .hybrid import (
    GRACEFUL_STRUCTURED_MISSES,
    GRACEFUL_VECTOR_MISSES,
    decide_strategy,
    should_fall_back_to_vector,
)
from .llm import LLMCompletion as AnswerLLMCompletion
from .llm import generate_response
from .prompt_builder import build_prompt
from .ranking import rank_results
from .rewriting import LLMCompletion as RewriteLLMCompletion
from .rewriting import rewrite_query
from .state import KnowledgeAgentState
from .structured_lookup import structured_lookup
from .types import RankedResult
from .vector_search import EmbeddingFunction, vector_search

logger = logging.getLogger(__name__)

Node = Callable[[KnowledgeAgentState], Awaitable[dict]]
StrategyEdge = Callable[[KnowledgeAgentState], list[str]]
# A conditional edge that picks exactly one successor, unlike StrategyEdge's
# fan-out list.
SingleEdge = Callable[[KnowledgeAgentState], str]

# next-node-name(s) for each strategy decide_strategy_edge might return;
# graph.py registers nodes under these exact names (see Module 16).
_STRATEGY_TO_NODE_NAMES: dict[str, tuple[str, ...]] = {
    "structured": ("structured_lookup",),
    "vector": ("vector_search",),
    "hybrid": ("structured_lookup", "vector_search"),
}

# Targets for the post-structured fallback edge. Named here for the same
# reason as the table above: graph.py registers nodes under these exact
# strings, and a typo would be a routing bug rather than an import error.
_VECTOR_SEARCH_NODE = "vector_search"
_RANK_NODE = "rank"


# ==========================================================================
# rewrite
# ==========================================================================


def make_rewrite_node(*, config: KnowledgeAgentConfig, llm_complete: RewriteLLMCompletion) -> Node:
    async def _rewrite_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        # to_thread: `llm_complete` is a blocking provider call (Groq SDK /
        # requests). This node is `async def`, so LangGraph runs it directly
        # on the event loop -- calling it inline would freeze every other
        # request in the process for the duration. See the module docstring.
        rewritten = await asyncio.to_thread(
            rewrite_query, state.raw_query, state.conversation_history, config=config, llm_complete=llm_complete
        )
        return {"rewritten_query": rewritten}

    return _rewrite_node


# ==========================================================================
# extract
# ==========================================================================


def make_extract_node(*, config: KnowledgeAgentConfig, llm_complete: ExtractionLLMCompletion) -> Node:
    async def _extract_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        structured_query = await asyncio.to_thread(
            extract_query, state.rewritten_query, config=config, llm_complete=llm_complete
        )
        return {"structured_query": structured_query}

    return _extract_node


# ==========================================================================
# decide_strategy (conditional edge, not a state-mutating node)
# ==========================================================================


def make_route_node(*, config: KnowledgeAgentConfig) -> Node:
    """Record the retrieval strategy in state before the fan-out (F5).

    `KnowledgeAgentState.retrieval_strategy` has been declared and
    documented as "set by the decide_strategy conditional edge" since the
    beginning, and was never actually set: LangGraph conditional edges
    return a route, they cannot write state. So the one decision that
    explains why two runs of the same question retrieved different things
    was invisible in every trace and every checkpoint — the variance could
    only be inferred from the answers.

    A node can write state, so the decision is made once here and the two
    conditional edges downstream read it instead of each recomputing it.
    """

    async def _route_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        strategy = decide_strategy(state.structured_query, config=config)
        query = state.structured_query
        # Ontology vocabulary and a number — no customer text, so this stays
        # safe to log while P0-4 (conversation state on stdout) is open.
        logger.info(
            "retrieval strategy=%s entity_type=%s attribute=%s relation=%s confidence=%.2f",
            strategy,
            getattr(query, "entity_type", None),
            getattr(query, "attribute", None),
            getattr(query, "relation_type", None),
            getattr(query, "confidence", 0.0),
        )
        return {"retrieval_strategy": strategy}

    return _route_node


def _strategy_of(state: KnowledgeAgentState, config: KnowledgeAgentConfig) -> str:
    """The strategy `route` recorded, recomputed only if it is missing.

    The fallback keeps the edges usable when a test drives them directly
    without running the `route` node first; in the compiled graph the value
    is always present.
    """
    return state.retrieval_strategy or decide_strategy(state.structured_query, config=config)


def make_decide_strategy_edge(*, config: KnowledgeAgentConfig) -> StrategyEdge:
    """Returns a LangGraph conditional-edge function: given the current
    state, returns the list of next node name(s) to route to — a single
    name for the pure structured/vector strategies, both node names for
    hybrid (LangGraph fans out to each and merges on the shared, but
    field-disjoint, KnowledgeAgentState)."""

    def _decide_strategy_edge(state: KnowledgeAgentState) -> list[str]:
        return list(_STRATEGY_TO_NODE_NAMES[_strategy_of(state, config)])

    return _decide_strategy_edge


def make_structured_fallback_edge(*, config: KnowledgeAgentConfig) -> SingleEdge:
    """Conditional edge after `structured_lookup`: retry semantically when an
    exact-fact lookup found nothing (P1-6).

    Without this, a *confident* extraction that names an entity type with no
    matching rows ended the retrieval phase with zero results, and the
    customer was told nothing was known about a question the semantic index
    could answer. Nothing errored — the graph reported success — so the only
    place the failure was visible was in the answer itself. See
    `hybrid.should_fall_back_to_vector` for the rule and the worked example.

    Reads the strategy the `route` node recorded (F5), so this edge and the
    fan-out edge can never disagree about which plan is running.

    That is what makes this safe for the hybrid strategy, which reaches this
    same edge: hybrid returns `"rank"` here because vector search is already
    running alongside, so the fallback can never double-run it.

    Since F5 removed structured-only as a routing outcome, this branch no
    longer fires. It is kept as a backstop against anything that routes
    there again — deleting it would mean re-introducing P1-6 the moment
    something did. See `hybrid.decide_strategy`.
    """

    def _structured_fallback_edge(state: KnowledgeAgentState) -> str:
        if should_fall_back_to_vector(_strategy_of(state, config), state.structured_facts):
            return _VECTOR_SEARCH_NODE
        return _RANK_NODE

    return _structured_fallback_edge


# ==========================================================================
# structured_lookup
# ==========================================================================


def make_structured_lookup_node(*, session_factory: async_sessionmaker[AsyncSession]) -> Node:
    async def _structured_lookup_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        async with session_factory() as session:
            try:
                facts = await structured_lookup(state.structured_query, session=session)
            except GRACEFUL_STRUCTURED_MISSES:
                facts = ()
        # Filtered here, at the point the dump is produced, rather than
        # downstream: ranking.py deliberately does not carry the originating
        # StructuredQuery forward, and context_builder.py deliberately does
        # not re-rank. An unslotted lookup returns everything known about the
        # entity, and shipping that to the prompt caused refusals on questions
        # the documentation answered (F4 — see fact_relevance.py).
        facts = relevant_facts(
            facts,
            query=state.structured_query,
            query_text=_rewritten_text(state),
        )
        return {"structured_facts": facts}

    return _structured_lookup_node


def _rewritten_text(state: KnowledgeAgentState) -> Optional[str]:
    """The question as retrieval saw it, falling back to the raw query when
    the rewrite stage was skipped (some tests drive nodes directly)."""
    rewritten = state.rewritten_query
    return getattr(rewritten, "rewritten_text", None) or state.raw_query


# ==========================================================================
# vector_search
# ==========================================================================


def make_vector_search_node(
    *, config: KnowledgeAgentConfig, session_factory: async_sessionmaker[AsyncSession], embed_query: EmbeddingFunction
) -> Node:
    async def _vector_search_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        async with session_factory() as session:
            try:
                chunks = await vector_search(state.rewritten_query, config=config, session=session, embed_query=embed_query)
            except GRACEFUL_VECTOR_MISSES:
                chunks = ()
        return {"retrieved_chunks": chunks}

    return _vector_search_node


# ==========================================================================
# rank
# ==========================================================================


def make_rank_node() -> Node:
    """No injected dependencies — rank_results() is pure and needs
    nothing beyond the data already in state."""

    async def _rank_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        pre_rank = RankedResult(structured_facts=state.structured_facts, retrieved_chunks=state.retrieved_chunks)
        return {"ranked_result": rank_results(pre_rank)}

    return _rank_node


# ==========================================================================
# deduplicate
# ==========================================================================


def make_deduplicate_node() -> Node:
    async def _deduplicate_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        return {"deduplicated_result": deduplicate(state.ranked_result)}

    return _deduplicate_node


# ==========================================================================
# build_context
# ==========================================================================


def make_build_context_node(*, config: KnowledgeAgentConfig) -> Node:
    async def _build_context_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        context = build_context(state.deduplicated_result, config=config)
        return {"context": context}

    return _build_context_node


# ==========================================================================
# build_prompt
# ==========================================================================


def make_build_prompt_node(*, config: KnowledgeAgentConfig) -> Node:
    async def _build_prompt_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        prompt = build_prompt(
            state.context,
            state.rewritten_query.rewritten_text,
            state.conversation_history,
            config=config,
        )
        return {"prompt": prompt}

    return _build_prompt_node


# ==========================================================================
# llm
# ==========================================================================


def make_llm_node(*, llm_complete: AnswerLLMCompletion) -> Node:
    async def _llm_node(state: KnowledgeAgentState) -> KnowledgeAgentState:
        response = await asyncio.to_thread(
            generate_response, state.prompt, context=state.context, llm_complete=llm_complete
        )
        return {"response": response}

    return _llm_node
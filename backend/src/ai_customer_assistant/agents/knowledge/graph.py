"""
graph.py — LangGraph StateGraph definition for the Knowledge Agent.

The only module besides nodes.py that imports `langgraph`. Everything
else in the package is framework-agnostic and independently testable
without a graph runtime — every test in this package's development so
far has proven that by construction.

Topology:

    rewrite -> extract -> [decide_strategy conditional edge]
        -> structured_lookup                          (structured)
        -> vector_search                               (vector)
        -> structured_lookup + vector_search (parallel) (hybrid)
    (structured_lookup | vector_search) -> rank -> deduplicate
        -> build_context -> build_prompt -> llm -> END

`decide_strategy`'s conditional edge (built by
`nodes.make_decide_strategy_edge`) returns a *list* of next node
names — one name for the pure strategies, both for hybrid — which is
exactly the LangGraph idiom for a conditional fan-out. Because
`structured_lookup` and `vector_search` write to disjoint state fields
(`structured_facts` vs `retrieved_chunks`) as partial-update dicts
(see nodes.py's module docstring for why that matters), running both
in parallel under the hybrid branch merges cleanly — verified against
a real compiled graph, not just asserted.

Both parallel branches converge on `rank` via two incoming edges;
LangGraph runs `rank` once both predecessors have completed for that
step, so ranking never sees a partially-populated state.

Concurrency note: the two retrieval nodes are given a `session_factory`
rather than a shared session, precisely because they can run
concurrently under the hybrid strategy — see nodes.py's module
docstring for the `InvalidRequestError` this avoids.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import KnowledgeAgentConfig
from .extraction import LLMCompletion as ExtractionLLMCompletion
from .llm import LLMCompletion as AnswerLLMCompletion
from .nodes import (
    make_build_context_node,
    make_build_prompt_node,
    make_decide_strategy_edge,
    make_deduplicate_node,
    make_extract_node,
    make_llm_node,
    make_rank_node,
    make_rewrite_node,
    make_structured_lookup_node,
    make_vector_search_node,
)
from .rewriting import LLMCompletion as RewriteLLMCompletion
from .state import KnowledgeAgentState
from .vector_search import EmbeddingFunction

_STRUCTURED_LOOKUP = "structured_lookup"
_VECTOR_SEARCH = "vector_search"


def build_knowledge_agent_graph(
    *,
    config: KnowledgeAgentConfig,
    rewrite_llm_complete: RewriteLLMCompletion,
    extraction_llm_complete: ExtractionLLMCompletion,
    answer_llm_complete: AnswerLLMCompletion,
    session_factory: async_sessionmaker[AsyncSession],
    embed_query: EmbeddingFunction,
) -> CompiledStateGraph:
    """Build and compile the Knowledge Agent's LangGraph graph.

    `session_factory` (an `async_sessionmaker`), not a bare session — the
    hybrid strategy runs `structured_lookup` and `vector_search`
    concurrently, and a single `AsyncSession` is not safe for concurrent
    use by two coroutines (see nodes.py's module docstring). Each
    retrieval node opens its own session, scoped to its own call, from
    this factory.

    Every other collaborator (config, the three LLM callables, the
    embedding function) is supplied by the caller — this function only
    wires them into node closures and assembles the graph topology. It
    contains no retrieval, ranking, or generation logic of its own."""
    graph = StateGraph(KnowledgeAgentState)

    graph.add_node("rewrite", make_rewrite_node(config=config, llm_complete=rewrite_llm_complete))
    graph.add_node("extract", make_extract_node(config=config, llm_complete=extraction_llm_complete))
    graph.add_node(_STRUCTURED_LOOKUP, make_structured_lookup_node(session_factory=session_factory))
    graph.add_node(
        _VECTOR_SEARCH,
        make_vector_search_node(config=config, session_factory=session_factory, embed_query=embed_query),
    )
    graph.add_node("rank", make_rank_node())
    graph.add_node("deduplicate", make_deduplicate_node())
    graph.add_node("build_context", make_build_context_node(config=config))
    graph.add_node("build_prompt", make_build_prompt_node(config=config))
    graph.add_node("llm", make_llm_node(llm_complete=answer_llm_complete))

    graph.set_entry_point("rewrite")
    graph.add_edge("rewrite", "extract")

    # Conditional fan-out: returns one or both of the two retrieval
    # node names depending on decide_strategy's structured/vector/
    # hybrid decision (see hybrid.py + nodes.py's routing table).
    graph.add_conditional_edges(
        "extract",
        make_decide_strategy_edge(config=config),
        [_STRUCTURED_LOOKUP, _VECTOR_SEARCH],
    )

    # Fan-in: both retrieval nodes converge on rank. When only one of
    # the two was actually reached (pure structured/vector strategy),
    # LangGraph runs rank once that single predecessor completes.
    graph.add_edge(_STRUCTURED_LOOKUP, "rank")
    graph.add_edge(_VECTOR_SEARCH, "rank")

    graph.add_edge("rank", "deduplicate")
    graph.add_edge("deduplicate", "build_context")
    graph.add_edge("build_context", "build_prompt")
    graph.add_edge("build_prompt", "llm")
    graph.add_edge("llm", END)

    return graph.compile()
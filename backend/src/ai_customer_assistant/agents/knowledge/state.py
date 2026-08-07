"""
state.py — KnowledgeAgentState: the LangGraph state schema.

The single frozen dataclass threaded through every node in graph.py.
Nothing in this file computes anything — `nodes.py` is the only place
that constructs a *new* state via `dataclasses.replace(state, field=...)`
after calling into a pure function from one of the stage modules
(rewriting.py, extraction.py, ...). No node ever mutates the state it
receives; each returns a distinct, fully-formed successor value.

Field lifecycle across the graph (see graph.py for exact wiring):

    raw_query, conversation_history      — set once, at graph entry
    rewritten_query                      — set by the `rewrite` node
    structured_query                     — set by the `extract` node
    retrieval_strategy                   — set by the `decide_strategy`
                                            conditional edge (hybrid.py)
    structured_facts                     — set by `structured_lookup` node
                                            (structured or hybrid path only)
    retrieved_chunks                     — set by `vector_search` node
                                            (vector or hybrid path only)
    ranked_result                        — set by the `rank` node
    deduplicated_result                  — set by the `deduplicate` node
    context                              — set by the `build_context` node
    prompt                               — set by the `build_prompt` node
    response                             — set by the `llm` node (terminal)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .types import (
    BuiltContext,
    BuiltPrompt,
    GroundedResponse,
    RankedResult,
    RetrievedChunk,
    RewrittenQuery,
    StructuredFact,
    StructuredQuery,
)


@dataclass(frozen=True)
class KnowledgeAgentState:
    """Immutable state passed between every LangGraph node in the
    Knowledge Agent graph. Every field beyond `raw_query` and
    `conversation_history` starts unset (`None` / empty tuple) and is
    populated exactly once, by exactly one node, as the graph
    progresses — never overwritten by a later node."""

    raw_query: str
    conversation_history: tuple[str, ...] = ()

    rewritten_query: Optional[RewrittenQuery] = None
    structured_query: Optional[StructuredQuery] = None
    retrieval_strategy: Optional[str] = None  # "structured" | "vector" | "hybrid"

    structured_facts: tuple[StructuredFact, ...] = ()
    retrieved_chunks: tuple[RetrievedChunk, ...] = ()

    ranked_result: Optional[RankedResult] = None
    deduplicated_result: Optional[RankedResult] = None

    context: Optional[BuiltContext] = None
    prompt: Optional[BuiltPrompt] = None
    response: Optional[GroundedResponse] = None
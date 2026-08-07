"""
types.py — immutable data layer for the Knowledge Agent.

Dataclasses only, no logic — mirrors the convention in
`chunk_embed/types.py`: every type is `frozen=True`, collections are
`tuple`s (never `list`), and any mapping field uses `MappingProxyType`
(never plain `dict`). Values pass through pure functions in the other
modules with zero risk of one stage mutating data another stage still
holds a reference to.

Nothing in this file validates, normalizes, or otherwise computes
anything — that behavior lives in the module that owns each stage
(rewriting.py, extraction.py, ontology.py, ...). This file only shapes
the data flowing between them.

Pipeline shape (see graph.py for the LangGraph wiring):

    RewrittenQuery
        -> StructuredQuery
            -> StructuredFact* (structured_lookup.py)
            -> RetrievedChunk* (vector_search.py)
        -> RankedResult (ranking.py, deduplication.py)
        -> BuiltContext (context_builder.py)
        -> BuiltPrompt (prompt_builder.py)
        -> GroundedResponse (llm.py)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, Optional

# --------------------------------------------------------------------------
# Shared literal aliases — kept here (not re-declared per-class) so every
# type that references them stays in sync with constants.py by construction.
# --------------------------------------------------------------------------

RetrievalStrategy = Literal["structured", "vector", "hybrid"]
ValueType = Literal["string", "number", "boolean", "date", "json"]
SourceType = Literal["FILE_UPLOAD", "EXTERNAL_INTEGRATION"]


# --------------------------------------------------------------------------
# Stage 1 — Query Rewriting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RewrittenQuery:
    """Output of `rewriting.rewrite_query()`. A retrieval-friendly restatement
    of the customer's message with conversational references resolved
    (e.g. "it" / "that project" -> the entity named earlier in history)."""

    original_text: str
    rewritten_text: str
    resolved_references: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Stage 2 — Structured Query Extraction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryFilter:
    """One additional constraint extracted alongside the primary
    entity/attribute/relation slots, e.g. a date range or status filter
    mentioned in the same query."""

    field: str
    value: str


@dataclass(frozen=True)
class StructuredQuery:
    """Output of `extraction.extract_query()`. Every populated field has
    already been normalized to a canonical ontology term via
    `ontology.py` — never raw user wording."""

    entity_type: Optional[str] = None
    entity_label: Optional[str] = None
    attribute: Optional[str] = None
    relation_type: Optional[str] = None
    filters: tuple[QueryFilter, ...] = ()
    confidence: float = 0.0


# --------------------------------------------------------------------------
# Stage 3a — Structured Lookup (EAV reads)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuredFact:
    """One deterministic fact resolved from `entity` / `attribute` /
    `value` / `relation`. Output of `structured_lookup.structured_lookup()`."""

    entity_id: str
    entity_type: str
    entity_label: str
    attribute: str
    value: str
    value_type: ValueType
    related_entity_label: Optional[str] = None
    relation_type: Optional[str] = None
    confidence: float = 1.0


# --------------------------------------------------------------------------
# Stage 3b — Vector Search
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkProvenance:
    """Provenance for one retrieved chunk, resolved by joining
    embedding_chunk -> knowledge_source_version -> knowledge_source
    -> knowledge_category (optional) -> entity (optional), per the
    mandated join contract. Every RetrievedChunk carries exactly one
    of these."""

    source_name: str
    source_type: SourceType
    category_name: Optional[str]
    version_number: int
    page: Optional[int]
    chunk_index: int
    entity_type: Optional[str] = None
    entity_label: Optional[str] = None


@dataclass(frozen=True)
class RetrievedChunk:
    """One semantically-similar chunk. Output of
    `vector_search.vector_search()`, scoped to the currently-live,
    INDEXED, active version per the join contract in vector_search.py."""

    chunk_id: str
    chunk_text: str
    similarity_score: float
    provenance: ChunkProvenance


# --------------------------------------------------------------------------
# Stage 4/5 — Ranking & Deduplication
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedResult:
    """The combined, ordered output of structured lookup and/or vector
    search, after ranking.rank_results() and deduplication.deduplicate()
    have each produced a new RankedResult (never mutated in place)."""

    structured_facts: tuple[StructuredFact, ...] = ()
    retrieved_chunks: tuple[RetrievedChunk, ...] = ()


# --------------------------------------------------------------------------
# Stage 6 — Context Construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltContext:
    """Output of `context_builder.build_context()`: the two prompt
    sections (Structured Facts, Relevant Documentation) as already-
    rendered text, plus the provenance list used to build citations
    later in llm.py."""

    structured_section: str
    documentation_section: str
    cited_provenance: tuple[ChunkProvenance, ...] = ()


# --------------------------------------------------------------------------
# Stage 7 — Prompt Construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltPrompt:
    """Output of `prompt_builder.build_prompt()`: the fully-rendered
    prompt ready to send to the LLM, plus the system instructions kept
    separate for providers that accept them as a distinct field."""

    system_instructions: str
    rendered_prompt: str


# --------------------------------------------------------------------------
# Stage 8 — LLM Generation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GroundedResponse:
    """Output of `llm.generate_response()`: the final answer plus
    whether it passed the groundedness check and the citations backing
    it, ready to hand back to the Supervisor Agent."""

    answer_text: str
    is_grounded: bool
    citations: tuple[ChunkProvenance, ...] = ()


# --------------------------------------------------------------------------
# Ontology support types (used by ontology.py's public API return values)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalizationResult:
    """Output of ontology.py's canonicalize_entity_type() /
    canonicalize_attribute(): the resolved canonical term plus a
    confidence score, so extraction.py can decide whether to trust an
    ambiguous mapping or fall back to asking a clarifying question."""

    canonical_term: str
    confidence: float
    alternate_candidates: tuple[str, ...] = ()
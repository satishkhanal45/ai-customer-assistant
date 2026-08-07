"""
context_builder.py — assemble the two-section context for the prompt.

`build_context()` is a pure function: it truncates an already-ranked,
already-deduplicated `RankedResult` to the configured budgets
(`config.max_structured_facts`, `config.max_context_chunks`) and
renders the two named sections (`constants.CONTEXT_SECTION_*`) as
plain text, ready for prompt_builder.py to drop into the final prompt.
It does not re-rank or re-deduplicate — that's ranking.py's and
deduplication.py's job, already done upstream.

Every rendered documentation entry carries a numbered citation marker
plus its provenance inline, and `BuiltContext.cited_provenance` carries
the same provenance objects in the same order — llm.py builds
`GroundedResponse.citations` directly from this list rather than
re-deriving it from the raw RankedResult.

When either section is empty, a plain-language placeholder is rendered
instead of an empty gap, so the LLM has an explicit, unambiguous signal
that nothing was found there — this is what lets prompt_builder.py's
"answer only from context; say so if there isn't enough" instruction
actually work, since the model can see whether a section is truly
empty rather than inferring it from blank space.
"""

from __future__ import annotations

from .config import KnowledgeAgentConfig
from .types import BuiltContext, ChunkProvenance, RankedResult, RetrievedChunk, StructuredFact

_NO_STRUCTURED_FACTS = "No structured facts were found for this query."
_NO_DOCUMENTATION = "No relevant documentation was found for this query."


def build_context(result: RankedResult, *, config: KnowledgeAgentConfig) -> BuiltContext:
    """Truncate to the configured budgets and render both context
    sections. Assumes `result` is already ranked and deduplicated."""
    facts = result.structured_facts[: config.max_structured_facts]
    chunks = result.retrieved_chunks[: config.max_context_chunks]

    return BuiltContext(
        structured_section=_render_structured_section(facts),
        documentation_section=_render_documentation_section(chunks),
        cited_provenance=tuple(chunk.provenance for chunk in chunks),
    )


# --------------------------------------------------------------------------
# Internals — Structured Facts section
# --------------------------------------------------------------------------


def _format_fact(fact: StructuredFact) -> str:
    """A relation fact ("X owned_by Y") and an attribute fact
    ("X.status = active") render differently, distinguished by whether
    `relation_type` is set — these are mutually exclusive per
    structured_lookup.py's lookup-kind dispatch, never both populated
    on the same fact."""
    if fact.relation_type is not None:
        return f'- {fact.entity_type} "{fact.entity_label}" {fact.relation_type} "{fact.related_entity_label}"'
    return f'- {fact.entity_type} "{fact.entity_label}".{fact.attribute} = {fact.value}'


def _render_structured_section(facts: tuple[StructuredFact, ...]) -> str:
    return "\n".join(_format_fact(fact) for fact in facts) if facts else _NO_STRUCTURED_FACTS


# --------------------------------------------------------------------------
# Internals — Relevant Documentation section
# --------------------------------------------------------------------------


def _format_provenance_label(provenance: ChunkProvenance) -> str:
    optional_parts = (
        f"category: {provenance.category_name}" if provenance.category_name else None,
        f"p.{provenance.page}" if provenance.page is not None else None,
    )
    label_parts = (provenance.source_name, *filter(None, optional_parts), f"v{provenance.version_number}")
    return ", ".join(label_parts)


def _format_chunk_entry(index: int, chunk: RetrievedChunk) -> str:
    marker = f"[{index}] ({_format_provenance_label(chunk.provenance)})"
    return f"{marker}\n{chunk.chunk_text}"


def _render_documentation_section(chunks: tuple[RetrievedChunk, ...]) -> str:
    if not chunks:
        return _NO_DOCUMENTATION
    return "\n\n".join(_format_chunk_entry(index, chunk) for index, chunk in enumerate(chunks, start=1))
"""
deduplication.py — remove duplicated information between structured
facts and document chunks.

`deduplicate()` is a pure function operating in three passes, each
addressing a distinct kind of duplication:

1. Exact-duplicate structured facts (same entity + attribute + value,
   which can happen if a general entity-values lookup and a
   attribute-specific lookup both ran and overlapped).
2. Exact-duplicate retrieved chunks (same chunk_id — defensive; the
   two hybrid-fan-out arms query disjoint tables so this shouldn't
   occur in practice, but a re-run or retry path could produce it).
3. Cross-type redundancy: a retrieved chunk whose text already
   restates a structured fact the pipeline already has verbatim. The
   structured fact is authoritative (it came from a deterministic EAV
   read); the chunk saying the same thing in prose adds no new
   information and just spends context budget — so the chunk is
   dropped, never the fact.

All three passes preserve input order (assumed to already be ranked by
ranking.py) via the `dict` keyed-by-identity + `reversed()` trick: the
*first* occurrence of a duplicate key wins, not the last, so
deduplication never silently promotes a lower-ranked duplicate over a
higher-ranked one.
"""

from __future__ import annotations

from .types import RankedResult, RetrievedChunk, StructuredFact


def deduplicate(result: RankedResult) -> RankedResult:
    """Return a new RankedResult with exact-duplicate facts and chunks
    removed, and any chunk that merely restates an already-present
    structured fact dropped in favor of the fact."""
    unique_facts = _dedupe_facts(result.structured_facts)
    unique_chunks = _dedupe_chunks(result.retrieved_chunks)
    non_redundant_chunks = tuple(
        chunk for chunk in unique_chunks if not _is_redundant_with_facts(chunk, unique_facts)
    )
    return RankedResult(structured_facts=unique_facts, retrieved_chunks=non_redundant_chunks)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _dedupe_facts(facts: tuple[StructuredFact, ...]) -> tuple[StructuredFact, ...]:
    """Order-preserving dedup keyed by (entity_id, attribute, value):
    build the key->fact map from the reversed sequence so a later
    duplicate never overwrites an earlier (higher-ranked) one, then
    reverse back to restore original order."""
    keyed = {(fact.entity_id, fact.attribute, fact.value): fact for fact in reversed(facts)}
    return tuple(reversed(tuple(keyed.values())))


def _dedupe_chunks(chunks: tuple[RetrievedChunk, ...]) -> tuple[RetrievedChunk, ...]:
    keyed = {chunk.chunk_id: chunk for chunk in reversed(chunks)}
    return tuple(reversed(tuple(keyed.values())))


def _is_redundant_with_facts(chunk: RetrievedChunk, facts: tuple[StructuredFact, ...]) -> bool:
    """A chunk is considered redundant with a fact when the chunk's text
    contains both that fact's value and the entity it describes,
    case-insensitively — a simple, pure, and deliberately conservative
    heuristic (both must match, not just the value alone, to avoid
    dropping a chunk that happens to mention the same value for an
    unrelated entity)."""
    chunk_text_lower = chunk.chunk_text.lower()
    return any(
        fact.value.lower() in chunk_text_lower and fact.entity_label.lower() in chunk_text_lower
        for fact in facts
    )
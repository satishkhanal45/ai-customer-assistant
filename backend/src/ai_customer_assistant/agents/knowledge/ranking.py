"""
ranking.py — rank retrieved results.

`rank_results()` is a pure function: it never re-queries anything, it
only reorders the `RankedResult` it's given. Structured facts and
retrieved chunks are ranked independently (they stay in separate
tuples all the way through to context_builder.py's two prompt
sections), each by a weighted score built from `constants.RANKING_WEIGHTS`.

Only three of RANKING_WEIGHTS' four keys apply here:
`exact_structured_match`, `semantic_similarity`, and `source_freshness`.
`extraction_confidence` already shaped *whether* structured facts exist
at all — it drove hybrid.py's strategy decision upstream — so reusing
it again here to re-weight facts that already exist would double-count
the same signal. RankedResult doesn't carry the originating
StructuredQuery forward, which is a deliberate shape choice: ranking
should score what was actually retrieved, not re-litigate how
confident extraction felt about asking for it.
"""

from __future__ import annotations

from .constants import RANKING_WEIGHTS
from .types import RankedResult, RetrievedChunk, StructuredFact


def rank_results(result: RankedResult) -> RankedResult:
    """Return a new RankedResult with `structured_facts` sorted by
    confidence and `retrieved_chunks` sorted by a similarity +
    freshness weighted score, both descending."""
    return RankedResult(
        structured_facts=tuple(sorted(result.structured_facts, key=_fact_score, reverse=True)),
        retrieved_chunks=tuple(sorted(result.retrieved_chunks, key=_chunk_score, reverse=True)),
    )


# --------------------------------------------------------------------------
# Internals — scoring
# --------------------------------------------------------------------------


def _fact_score(fact: StructuredFact) -> float:
    """Structured facts come from a deterministic EAV lookup, so their
    only meaningful ranking signal is the lookup's own confidence
    (1.0 for an exact match; fuzzy-matched facts, if ever introduced,
    would score lower)."""
    return RANKING_WEIGHTS["exact_structured_match"] * fact.confidence


def _freshness_score(version_number: int) -> float:
    """Pure, monotonically increasing in [0, 1) as version_number grows.
    RetrievedChunk carries no absolute timestamp (only version_number,
    page, chunk_index — see ChunkProvenance), so version_number is used
    as a lightweight recency proxy: a chunk from a document's 5th
    re-index outranks one from its 1st, all else equal."""
    return version_number / (version_number + 1)


def _chunk_score(chunk: RetrievedChunk) -> float:
    similarity_component = RANKING_WEIGHTS["semantic_similarity"] * chunk.similarity_score
    freshness_component = RANKING_WEIGHTS["source_freshness"] * _freshness_score(chunk.provenance.version_number)
    return similarity_component + freshness_component
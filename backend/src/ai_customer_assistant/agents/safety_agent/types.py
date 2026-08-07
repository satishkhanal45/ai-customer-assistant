"""
Data layer for the groundedness check (Safety Agent).

Contains ONLY data definitions — no business logic, no I/O, no side
effects. Immutable throughout, matching the project's existing
ticket_agent module pattern.

Contract boundaries:
    SentenceScore   -> one answer sentence's best-matching-chunk
                       similarity, plus whether it individually cleared
                       the per-sentence threshold.
    GroundednessResult -> the final verdict for a whole answer: the
                       binary decision, the confidence score (fraction
                       of sentences that passed), and the full
                       per-sentence breakdown for logging/debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SentenceScore:
    """
    One answer sentence's similarity result against the retrieved chunks.

    Attributes:
        sentence: The sentence text, as split from the full answer.
        best_similarity: The highest cosine similarity found between
                          this sentence's embedding and any retrieved
                          chunk's embedding.
        passed: Whether best_similarity met or exceeded the configured
                per-sentence threshold.
    """

    sentence: str
    best_similarity: float
    passed: bool


@dataclass(frozen=True, slots=True)
class GroundednessResult:
    """
    The full groundedness verdict for one generated answer.

    Attributes:
        is_grounded: The binary decision — True if the fraction of
                     passed sentences met or exceeded the configured
                     aggregation cutoff.
        confidence_score: Fraction of sentences that passed (0.0-1.0).
                           This is deliberately the same number driving
                           is_grounded, expressed as a score rather than
                           a yes/no, so the two never disagree with each
                           other.
        sentence_scores: Per-sentence breakdown, in answer order —
                          kept for logging, debugging, and future
                          threshold tuning. Not required by callers that
                          only need the binary decision.
    """

    is_grounded: bool
    confidence_score: float
    sentence_scores: tuple[SentenceScore, ...] = field(default_factory=tuple)

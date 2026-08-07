"""
Groundedness check (Safety Agent): generated answer -> GroundednessResult.

Approach (per project decision): embedding similarity, not an LLM judge
or a dedicated entailment model — the project has no LLM client set up
yet, and this reuses the BGE embedding model already loaded elsewhere in
the pipeline (see ingestion/chunk_embed/embedding.py), so no new
dependency is introduced.

Per-sentence, not whole-answer (per project decision): the generated
answer is split into individual sentences, and EACH sentence is checked
against the retrieved chunks independently. This catches a single
hallucinated sentence hiding inside an otherwise well-supported answer —
a failure mode whole-answer comparison can miss, since a few strongly
matching sentences can average out one weak one.

Binary decision rule (per project decision): "at least X% of sentences
must meet the per-sentence threshold" — not "all sentences" (too
strict; even one odd phrasing fails the whole answer) and not "average
similarity" (one strong sentence can mask one hallucinated sentence,
the exact failure mode per-sentence mode exists to catch).

Confidence score (per project decision): deliberately just the fraction
of sentences that passed — the SAME number driving the binary decision,
expressed as a score instead of a yes/no. This guarantees the score and
the decision never disagree with each other, unlike e.g. an average
raw-similarity score, which could tell a different story than the
binary outcome.

Filler/short sentences (per project decision): NOT special-cased.
Boilerplate phrasing (e.g. "Here's what I found:") is scored and
counted like any other sentence, even though it will typically score
low against retrieved chunks. This is a known, accepted tradeoff, not
an oversight — see project decision log.

Sentence splitting (per project decision): a simple, dependency-free
split on .!? followed by whitespace — NOT a real NLP sentence
tokenizer (nltk/spacy), since the project has no such dependency yet
and the "no new dependency" pattern was set by the ticket agent's email
validation choice. Known limitation: this will incorrectly split
abbreviations like "Dr. Smith" or "e.g." into extra fragments. Accepted
as a minor tradeoff for a support-answer context; swapping in a real
sentence tokenizer later only requires changing split_into_sentences()
— nothing else in this module depends on how splitting happens.
"""

from __future__ import annotations

import math
import re

from sentence_transformers import SentenceTransformer

from agents.safety_agent.types import GroundednessResult, SentenceScore
from ingestion.chunk_embed.types import EmbeddedChunk

# Default per-sentence similarity threshold and aggregation cutoff, per
# project decision. Both are plain function defaults (not hardcoded
# deep inside the logic) so they can be overridden per-call without
# editing this module — e.g. for tuning against real traffic later.
DEFAULT_SENTENCE_THRESHOLD = 0.75
DEFAULT_AGGREGATION_CUTOFF = 0.80

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def split_into_sentences(answer: str) -> tuple[str, ...]:
    """
    Split ``answer`` into sentences on '.', '!', or '?' followed by
    whitespace.

    Known limitation (accepted, see module docstring): mis-splits
    abbreviations like "Dr. Smith" or "e.g." into extra fragments.

    Args:
        answer: The full generated answer text.

    Returns:
        An immutable tuple of non-empty, whitespace-trimmed sentences,
        in original order. Empty input returns an empty tuple.
    """
    if not answer.strip():
        return ()

    return tuple(
        sentence.strip()
        for sentence in _SENTENCE_BOUNDARY.split(answer.strip())
        if sentence.strip()
    )


def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """
    Cosine similarity between two equal-length vectors.

    Computed generally (dot product over the product of norms) rather
    than assuming both inputs are pre-normalized, even though in
    practice both the chunk embeddings (see embedding.py,
    normalize_embeddings=True) and the sentence embeddings produced by
    _embed_sentences() below are normalized — this keeps the function
    correct on its own terms rather than silently depending on a
    caller's normalization choice.

    Returns 0.0 for a zero-length vector on either side, rather than
    raising a division-by-zero error — an all-zero embedding is a
    degenerate case, not something this comparison should crash on.
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _best_similarity(
    sentence_embedding: tuple[float, ...], chunks: tuple[EmbeddedChunk, ...]
) -> float:
    """
    The highest cosine similarity between ``sentence_embedding`` and
    any chunk's embedding in ``chunks``.

    Returns 0.0 if ``chunks`` is empty — a sentence cannot be grounded
    against zero retrieved context, so it correctly scores as
    unsupported rather than raising on an empty sequence.
    """
    if not chunks:
        return 0.0
    return max(_cosine_similarity(sentence_embedding, chunk.embedding) for chunk in chunks)


def _embed_sentences(
    sentences: tuple[str, ...], model: SentenceTransformer
) -> tuple[tuple[float, ...], ...]:
    """
    Embed all ``sentences`` in a single batched call, normalized —
    matching the normalization convention already used for chunk
    embeddings elsewhere in the pipeline (see embedding.py), so
    cosine similarity between a sentence and a chunk is comparing two
    vectors on the same footing.
    """
    raw_vectors = model.encode(
        list(sentences), normalize_embeddings=True, convert_to_numpy=True
    )
    return tuple(tuple(float(component) for component in vector) for vector in raw_vectors)


def check_groundedness(
    answer: str,
    retrieved_chunks: tuple[EmbeddedChunk, ...],
    *,
    embedding_model: SentenceTransformer,
    sentence_threshold: float = DEFAULT_SENTENCE_THRESHOLD,
    aggregation_cutoff: float = DEFAULT_AGGREGATION_CUTOFF,
) -> GroundednessResult:
    """
    Decide whether ``answer`` is grounded in ``retrieved_chunks``.

    Per-sentence embedding similarity, per project decision — see
    module docstring for the full reasoning behind each design choice
    below.

    Args:
        answer: The generated answer text to check.
        retrieved_chunks: The chunks that were retrieved and used to
                           build the prompt for this answer, each
                           already carrying its own embedding (see
                           ingestion/chunk_embed/types.py:EmbeddedChunk).
        embedding_model: A loaded SentenceTransformer instance (see
                          embedding.get_embedding_model), passed in
                          explicitly by the caller — this module does
                          not load its own model, consistent with the
                          project's explicit-dependency-injection
                          pattern already used in tokenizer.py and
                          embedding.py.
        sentence_threshold: Minimum cosine similarity for a single
                             sentence to count as "passed" (default
                             0.75, per project decision).
        aggregation_cutoff: Minimum fraction of sentences that must
                             pass for the whole answer to be grounded
                             (default 0.80, per project decision).

    Returns:
        A GroundednessResult with the binary decision, the confidence
        score (fraction of sentences passed), and the full per-sentence
        breakdown.

        Edge case — empty answer: returns is_grounded=False,
        confidence_score=0.0, sentence_scores=(). An answer with no
        content cannot be considered grounded in anything.
    """
    sentences = split_into_sentences(answer)
    if not sentences:
        return GroundednessResult(is_grounded=False, confidence_score=0.0, sentence_scores=())

    sentence_embeddings = _embed_sentences(sentences, embedding_model)

    sentence_scores = tuple(
        SentenceScore(
            sentence=sentence,
            best_similarity=(similarity := _best_similarity(embedding, retrieved_chunks)),
            passed=similarity >= sentence_threshold,
        )
        for sentence, embedding in zip(sentences, sentence_embeddings)
    )

    passed_count = sum(score.passed for score in sentence_scores)
    confidence_score = passed_count / len(sentence_scores)

    return GroundednessResult(
        is_grounded=confidence_score >= aggregation_cutoff,
        confidence_score=confidence_score,
        sentence_scores=sentence_scores,
    )

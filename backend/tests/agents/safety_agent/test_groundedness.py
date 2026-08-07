"""
Unit tests for agents/safety_agent/groundedness/.

No real embedding model is loaded: a deterministic FakeEmbeddingModel
test double (same pattern as conftest.py's FakeEmbeddingModel for the
ingestion pipeline) stands in, so these tests run offline with no
network access or model download.
"""

from __future__ import annotations

import dataclasses

import pytest

from agents.safety_agent.groundedness import (
    DEFAULT_AGGREGATION_CUTOFF,
    DEFAULT_SENTENCE_THRESHOLD,
    check_groundedness,
    split_into_sentences,
)
from agents.safety_agent.types import GroundednessResult, SentenceScore
from ingestion.chunk_embed.types import Chunk, EmbeddedChunk


class FakeEmbeddingModel:
    """
    Deterministic test double: returns a pre-configured vector for each
    exact sentence text it's asked to encode. Raises KeyError (a clear,
    obvious test failure) if asked to encode an unexpected sentence,
    rather than silently returning something wrong.
    """

    def __init__(self, vectors_by_text: dict[str, tuple[float, ...]]) -> None:
        self.vectors_by_text = vectors_by_text
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], **kwargs: object) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        return [self.vectors_by_text[text] for text in texts]


def make_chunk(text: str, embedding: tuple[float, ...]) -> EmbeddedChunk:
    return EmbeddedChunk(
        chunk=Chunk(source_id="doc-1", chunk_index=0, text=text, token_count=1),
        embedding=embedding,
    )


class TestSplitIntoSentences:
    def test_splits_on_period(self) -> None:
        assert split_into_sentences("First. Second.") == ("First.", "Second.")

    def test_splits_on_exclamation_and_question_mark(self) -> None:
        assert split_into_sentences("Wow! Really?") == ("Wow!", "Really?")

    def test_empty_string_returns_empty_tuple(self) -> None:
        assert split_into_sentences("") == ()

    def test_whitespace_only_returns_empty_tuple(self) -> None:
        assert split_into_sentences("   \n  ") == ()

    def test_single_sentence_no_split(self) -> None:
        assert split_into_sentences("Just one sentence.") == ("Just one sentence.",)

    def test_known_limitation_abbreviation_mis_split(self) -> None:
        # Documented, accepted limitation — not a bug: "Dr." incorrectly
        # ends up as its own fragment.
        assert split_into_sentences("Dr. Smith agreed.") == ("Dr.", "Smith agreed.")


class TestCheckGroundednessWellSupportedAnswer:
    def test_all_sentences_matching_is_grounded(self) -> None:
        model = FakeEmbeddingModel(
            {
                "Founded in 2019.": (1.0, 0.0),
                "Twelve artists on staff.": (0.0, 1.0),
            }
        )
        chunks = (
            make_chunk("founding chunk", (1.0, 0.0)),
            make_chunk("staff chunk", (0.0, 1.0)),
        )
        result = check_groundedness(
            "Founded in 2019. Twelve artists on staff.", chunks, embedding_model=model
        )
        assert result.is_grounded is True
        assert result.confidence_score == 1.0
        assert all(score.passed for score in result.sentence_scores)


class TestCheckGroundednessHallucination:
    def test_one_hallucinated_sentence_out_of_three_fails_cutoff(self) -> None:
        # 2/3 = 0.667, below the default 0.80 cutoff -> should be ungrounded,
        # even though 2 of 3 sentences are strongly supported.
        model = FakeEmbeddingModel(
            {
                "Founded in 2019.": (1.0, 0.0, 0.0),
                "Twelve artists on staff.": (0.0, 1.0, 0.0),
                "Prices start at $500.": (0.0, 0.0, 1.0),
            }
        )
        chunks = (
            make_chunk("founding chunk", (1.0, 0.0, 0.0)),
            make_chunk("staff chunk", (0.0, 1.0, 0.0)),
            make_chunk("unrelated chunk", (0.1, 0.1, 0.05)),
        )
        result = check_groundedness(
            "Founded in 2019. Twelve artists on staff. Prices start at $500.",
            chunks,
            embedding_model=model,
        )
        assert result.is_grounded is False
        assert result.confidence_score == pytest.approx(2 / 3)
        assert result.sentence_scores[0].passed is True
        assert result.sentence_scores[1].passed is True
        assert result.sentence_scores[2].passed is False

    def test_confidence_score_equals_fraction_passed(self) -> None:
        # Confirms confidence_score is exactly passed_count / total —
        # not a raw similarity average, per project decision.
        model = FakeEmbeddingModel(
            {
                "Good sentence.": (1.0, 0.0),
                "Bad sentence.": (0.0, 1.0),
            }
        )
        chunks = (make_chunk("chunk", (1.0, 0.0)),)  # only supports "Good sentence."
        result = check_groundedness("Good sentence. Bad sentence.", chunks, embedding_model=model)
        assert result.confidence_score == 0.5


class TestCheckGroundednessEdgeCases:
    def test_empty_answer_is_ungrounded(self) -> None:
        model = FakeEmbeddingModel({})
        result = check_groundedness("", (make_chunk("x", (1.0,)),), embedding_model=model)
        assert result.is_grounded is False
        assert result.confidence_score == 0.0
        assert result.sentence_scores == ()

    def test_whitespace_only_answer_is_ungrounded(self) -> None:
        model = FakeEmbeddingModel({})
        result = check_groundedness("   ", (make_chunk("x", (1.0,)),), embedding_model=model)
        assert result.is_grounded is False

    def test_no_retrieved_chunks_is_ungrounded(self) -> None:
        model = FakeEmbeddingModel({"Some claim.": (1.0, 0.0)})
        result = check_groundedness("Some claim.", (), embedding_model=model)
        assert result.is_grounded is False
        assert result.sentence_scores[0].best_similarity == 0.0

    def test_empty_model_does_not_call_encode_for_empty_answer(self) -> None:
        model = FakeEmbeddingModel({})
        check_groundedness("", (make_chunk("x", (1.0,)),), embedding_model=model)
        assert model.calls == []


class TestCheckGroundednessBatching:
    def test_all_sentences_embedded_in_a_single_call(self) -> None:
        model = FakeEmbeddingModel(
            {
                "One.": (1.0, 0.0),
                "Two.": (0.0, 1.0),
                "Three.": (1.0, 1.0),
            }
        )
        chunks = (make_chunk("chunk", (1.0, 0.0)),)
        check_groundedness("One. Two. Three.", chunks, embedding_model=model)
        assert len(model.calls) == 1
        assert model.calls[0] == ["One.", "Two.", "Three."]


class TestThresholdOverrides:
    def test_custom_sentence_threshold_is_respected(self) -> None:
        # A weak match (0.5 similarity) fails the default 0.75 threshold
        # but should pass a relaxed 0.4 threshold.
        model = FakeEmbeddingModel({"Weak match.": (1.0, 1.0)})
        chunks = (make_chunk("chunk", (1.0, 0.0)),)  # cosine sim = ~0.707

        default_result = check_groundedness("Weak match.", chunks, embedding_model=model)
        relaxed_result = check_groundedness(
            "Weak match.", chunks, embedding_model=model, sentence_threshold=0.5
        )

        # cosine((1,1), (1,0)) ~= 0.707 -- below the default 0.75 threshold,
        # above the relaxed 0.5 threshold.
        assert default_result.sentence_scores[0].passed is False
        assert relaxed_result.sentence_scores[0].passed is True

    def test_custom_aggregation_cutoff_is_respected(self) -> None:
        model = FakeEmbeddingModel(
            {"Good.": (1.0, 0.0), "Bad.": (0.0, 1.0)}
        )
        chunks = (make_chunk("chunk", (1.0, 0.0)),)

        strict_result = check_groundedness(
            "Good. Bad.", chunks, embedding_model=model, aggregation_cutoff=0.9
        )
        lenient_result = check_groundedness(
            "Good. Bad.", chunks, embedding_model=model, aggregation_cutoff=0.5
        )

        assert strict_result.is_grounded is False   # 0.5 < 0.9
        assert lenient_result.is_grounded is True    # 0.5 >= 0.5


class TestDefaultsMatchProjectDecision:
    def test_default_sentence_threshold_is_0_75(self) -> None:
        assert DEFAULT_SENTENCE_THRESHOLD == 0.75

    def test_default_aggregation_cutoff_is_0_80(self) -> None:
        assert DEFAULT_AGGREGATION_CUTOFF == 0.80


class TestImmutability:
    def test_sentence_score_is_frozen(self) -> None:
        score = SentenceScore(sentence="x", best_similarity=0.5, passed=False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            score.passed = True  # type: ignore[misc]

    def test_groundedness_result_is_frozen(self) -> None:
        result = GroundednessResult(is_grounded=True, confidence_score=1.0, sentence_scores=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.is_grounded = False  # type: ignore[misc]

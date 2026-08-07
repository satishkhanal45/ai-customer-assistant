"""Unit tests for ingestion/embedding.py."""

from __future__ import annotations

import pytest

from ingestion.chunk_embed.embedding import (
    EmbeddingDimensionMismatchError,
    EmbeddingGenerationError,
    EmptyChunkTextError,
    embed_chunks,
)
from tests.chunk_embed.conftest import FakeEmbeddingModel, make_chunk


class TestEmptyInput:
    def test_empty_tuple_returns_empty_tuple(self, fake_embedding_model) -> None:
        result = embed_chunks(
            (), model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert result == ()

    def test_empty_tuple_does_not_call_the_model(self, fake_embedding_model) -> None:
        embed_chunks(
            (), model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert fake_embedding_model.calls == []


class TestSuccessfulGeneration:
    def test_generates_one_embedding_per_chunk(self, fake_embedding_model) -> None:
        chunks = (make_chunk(chunk_index=0, text="alpha"), make_chunk(chunk_index=1, text="beta gamma"))
        result = embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert len(result) == 2

    def test_embedding_dimension_is_exactly_768(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="alpha"),)
        result = embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert len(result[0].embedding) == 768

    def test_order_is_preserved(self, fake_embedding_model) -> None:
        chunks = tuple(make_chunk(chunk_index=i, text=f"chunk {i}") for i in range(5))
        result = embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert [ec.chunk.chunk_index for ec in result] == [0, 1, 2, 3, 4]
        assert all(ec.chunk is chunk for ec, chunk in zip(result, chunks))

    def test_normalize_embeddings_flag_is_passed_through(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="alpha"),)
        embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert fake_embedding_model.calls[0]["kwargs"]["normalize_embeddings"] is True

    def test_normalize_embeddings_false_is_passed_through(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="alpha"),)
        embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=False, expected_dimension=768
        )
        assert fake_embedding_model.calls[0]["kwargs"]["normalize_embeddings"] is False

    def test_batch_size_passthrough(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="alpha"),)
        embed_chunks(
            chunks,
            model=fake_embedding_model,
            normalize_embeddings=True,
            expected_dimension=768,
            batch_size=16,
        )
        assert fake_embedding_model.calls[0]["kwargs"]["batch_size"] == 16

    def test_batch_size_omitted_when_not_provided(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="alpha"),)
        embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert "batch_size" not in fake_embedding_model.calls[0]["kwargs"]

    def test_all_texts_sent_in_a_single_batched_call(self, fake_embedding_model) -> None:
        chunks = tuple(make_chunk(chunk_index=i, text=f"chunk {i}") for i in range(10))
        embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert len(fake_embedding_model.calls) == 1
        assert fake_embedding_model.calls[0]["texts"] == [f"chunk {i}" for i in range(10)]

    def test_output_is_immutable_tuples(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="alpha"),)
        result = embed_chunks(
            chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
        )
        assert isinstance(result, tuple)
        assert isinstance(result[0].embedding, tuple)


class TestInvalidInput:
    def test_empty_text_chunk_raises(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text=""),)
        with pytest.raises(EmptyChunkTextError):
            embed_chunks(
                chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
            )

    def test_whitespace_only_text_chunk_raises(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text="   \n\t  "),)
        with pytest.raises(EmptyChunkTextError):
            embed_chunks(
                chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
            )

    def test_invalid_chunk_does_not_call_the_model(self, fake_embedding_model) -> None:
        chunks = (make_chunk(text=""),)
        with pytest.raises(EmptyChunkTextError):
            embed_chunks(
                chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
            )
        assert fake_embedding_model.calls == []

    def test_error_message_identifies_offending_chunk_index(self, fake_embedding_model) -> None:
        chunks = (make_chunk(chunk_index=0, text="ok"), make_chunk(chunk_index=1, text=""))
        with pytest.raises(EmptyChunkTextError, match="1"):
            embed_chunks(
                chunks, model=fake_embedding_model, normalize_embeddings=True, expected_dimension=768
            )


class TestEmbeddingFailures:
    def test_model_exception_is_wrapped(self) -> None:
        failing_model = FakeEmbeddingModel(fail=True)
        chunks = (make_chunk(text="alpha"),)
        with pytest.raises(EmbeddingGenerationError):
            embed_chunks(
                chunks, model=failing_model, normalize_embeddings=True, expected_dimension=768
            )

    def test_original_exception_is_chained(self) -> None:
        failing_model = FakeEmbeddingModel(fail=True)
        chunks = (make_chunk(text="alpha"),)
        with pytest.raises(EmbeddingGenerationError) as exc_info:
            embed_chunks(
                chunks, model=failing_model, normalize_embeddings=True, expected_dimension=768
            )
        assert isinstance(exc_info.value.__cause__, RuntimeError)


class TestDimensionMismatch:
    def test_wrong_dimension_raises(self) -> None:
        wrong_dim_model = FakeEmbeddingModel(wrong_dimension=True)
        chunks = (make_chunk(text="alpha"),)
        with pytest.raises(EmbeddingDimensionMismatchError):
            embed_chunks(
                chunks, model=wrong_dim_model, normalize_embeddings=True, expected_dimension=768
            )

    def test_error_message_identifies_actual_dimension(self) -> None:
        wrong_dim_model = FakeEmbeddingModel(dimension=768, wrong_dimension=True)
        chunks = (make_chunk(text="alpha"),)
        with pytest.raises(EmbeddingDimensionMismatchError, match="767"):
            embed_chunks(
                chunks, model=wrong_dim_model, normalize_embeddings=True, expected_dimension=768
            )


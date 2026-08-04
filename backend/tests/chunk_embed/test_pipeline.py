"""
Integration tests for ingestion/pipeline.py.

Verifies the full in-scope flow this module owns:
    extracted text -> chunking -> embedding generation
Database storage is deliberately NOT part of this integration test —
storage.py does not exist yet (blocked on the embedding_chunk schema),
so "verify every stage" here means every stage that exists so far.
A storage-inclusive integration test should be added once storage.py
lands.
"""

from __future__ import annotations

import pytest

from ingestion.chunk_embed.chunking import UnknownSourceTypeError
from ingestion.chunk_embed.embedding import EmbeddingGenerationError
from ingestion.chunk_embed.pipeline import process_document
from ingestion.chunk_embed.types import HeadingMarker
from backend.tests.chunk_embed.conftest import FakeEmbeddingModel, make_document


class TestFullFlow:
    def test_extracted_document_produces_embedded_chunks(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        text = "Intro words here. ### Liability Coverage clause details follow after this heading."
        position = text.index("### Liability Coverage")
        structure = (HeadingMarker(heading_text="### Liability Coverage", level=3, position=position),)
        document = make_document(
            source_id="doc-1", text=text, structure=structure, metadata={"title": "Policy"}
        )

        result = process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
        )

        # Stage 1 verified: chunking produced 2 sections (preamble + heading section).
        assert len(result) == 2
        assert result[0].chunk.text == text[:position]
        assert result[1].chunk.text.startswith("### Liability Coverage")

        # Stage 2 verified: every chunk has a correctly-shaped embedding.
        assert all(len(ec.embedding) == settings.embedding_dimension for ec in result)

        # Traceability preserved end-to-end.
        assert all(ec.chunk.source_id == "doc-1" for ec in result)
        assert [ec.chunk.chunk_index for ec in result] == [0, 1]

    def test_structured_document_produces_exactly_one_embedded_chunk(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(source_type="csv", text="name,age\njane,30", structure=None)

        result = process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
        )

        assert len(result) == 1
        assert result[0].chunk.chunk_index == 0

    def test_empty_document_produces_no_embedded_chunks_and_does_not_call_model(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="")

        result = process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
        )

        assert result == ()
        assert fake_embedding_model.calls == []

    def test_large_document_chunked_and_embedded_in_one_batched_call(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(1200))
        document = make_document(text=text)

        result = process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
        )

        assert len(result) > 1
        # embed_chunks batches all chunk texts into a single model.encode() call.
        assert len(fake_embedding_model.calls) == 1
        assert len(fake_embedding_model.calls[0]["texts"]) == len(result)


class TestSettingsAreThreadedThrough:
    def test_normalize_embeddings_flag_reaches_the_model(
        self, word_tokenizer, fake_embedding_model, long_form_source_types, structured_source_types
    ) -> None:
        from ingestion.chunk_embed.config import IngestionSettings

        settings = IngestionSettings(_env_file=None, normalize_embeddings=False)
        document = make_document(text="hello world")

        process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
        )

        assert fake_embedding_model.calls[0]["kwargs"]["normalize_embeddings"] is False

    def test_chunk_size_and_overlap_are_used_for_splitting(
        self, word_tokenizer, fake_embedding_model, long_form_source_types, structured_source_types
    ) -> None:
        from ingestion.chunk_embed.config import IngestionSettings

        settings = IngestionSettings(_env_file=None, chunk_size_tokens=100, chunk_overlap_tokens=10)
        text = " ".join(f"word{i}" for i in range(250))
        document = make_document(text=text)

        result = process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
        )

        assert all(ec.chunk.token_count <= 100 for ec in result)

    def test_embedding_batch_size_forwarded(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="hello world")

        process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
            embedding_batch_size=8,
        )

        assert fake_embedding_model.calls[0]["kwargs"]["batch_size"] == 8

    def test_resolve_page_range_forwarded(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        def resolver(start: int, end: int) -> dict[str, object]:
            return {"page_start": start, "page_end": end}

        document = make_document(text="hello world")

        result = process_document(
            document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=long_form_source_types,
            structured_source_types=structured_source_types,
            resolve_page_range=resolver,
        )

        assert result[0].chunk.metadata["page_start"] == 0
        assert result[0].chunk.metadata["page_end"] == len("hello world")


class TestErrorsPropagateUnwrapped:
    def test_unknown_source_type_propagates_from_chunking(
        self, word_tokenizer, fake_embedding_model, settings, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(source_type="mystery_type", text="some text")

        with pytest.raises(UnknownSourceTypeError):
            process_document(
                document,
                settings=settings,
                tokenizer=word_tokenizer,
                embedding_model=fake_embedding_model,
                long_form_source_types=long_form_source_types,
                structured_source_types=structured_source_types,
            )

        # Chunking failed before embedding was ever attempted.
        assert fake_embedding_model.calls == []

    def test_embedding_failure_propagates_from_embedding_stage(
        self, word_tokenizer, settings, long_form_source_types, structured_source_types
    ) -> None:
        failing_model = FakeEmbeddingModel(fail=True)
        document = make_document(text="hello world")

        with pytest.raises(EmbeddingGenerationError):
            process_document(
                document,
                settings=settings,
                tokenizer=word_tokenizer,
                embedding_model=failing_model,
                long_form_source_types=long_form_source_types,
                structured_source_types=structured_source_types,
            )


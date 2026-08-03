"""
Integration tests run against a real extracted document instead of the
short synthetic strings used elsewhere in the suite.

Source: tests/fixtures/alpinist_studios_handbook.txt — plain text pulled
from the 31 text-bearing pages of the "Synthetic_data.pdf" company
handbook (page 16 was image-only and contributed no text). ~5,900 words,
no markdown/heading markup, so structure=None on the ExtractedDocument —
per chunking.py's documented behavior, that's a supported input, not a
missing feature: it falls straight through to recursive token-window
splitting rather than structure-aware sectioning. If structure-aware
splitting needs to be exercised against real data later, a HeadingMarker
tuple can be layered on top of the same fixture text without touching
these tests.

Still uses the deterministic WordTokenizer / FakeEmbeddingModel test
doubles from conftest.py, not the real BGE tokenizer/model — this
environment has no network access to download them. Swap in
tokenizer.get_tokenizer(...) / embedding.get_embedding_model(...) to run
this same file against the real model where network access is available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.chunk_embed.chunking import chunk_document
from ingestion.chunk_embed.pipeline import process_document
from ingestion.chunk_embed.types import ExtractedDocument

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "alpinist_studios_handbook.txt"


@pytest.fixture(scope="session")
def real_document_text() -> str:
    """Full text of the real handbook document, loaded once per test run."""
    return _FIXTURE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def real_document(real_document_text: str) -> ExtractedDocument:
    """
    The real handbook as an ExtractedDocument, source_type='docx' (a
    long-form type), structure=None since no heading extraction has been
    run on it yet.
    """
    return ExtractedDocument(
        source_id="alpinist-studios-handbook",
        source_type="docx",
        text=real_document_text,
        structure=None,
        metadata={"title": "Alpinist Studios Company Handbook"},
    )


@pytest.fixture
def real_document_long_form_source_types() -> frozenset[str]:
    return frozenset({"docx"})


class TestChunkingRealDocument:
    def test_produces_multiple_chunks(
        self, real_document, word_tokenizer, settings, real_document_long_form_source_types
    ) -> None:
        chunks = chunk_document(
            real_document,
            tokenizer=word_tokenizer,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert len(chunks) > 1

    def test_chunk_indices_are_sequential_in_document_order(
        self, real_document, word_tokenizer, settings, real_document_long_form_source_types
    ) -> None:
        chunks = chunk_document(
            real_document,
            tokenizer=word_tokenizer,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_chunks_cover_the_full_text_from_first_to_last_word(
        self, real_document, word_tokenizer, settings, real_document_long_form_source_types
    ) -> None:
        chunks = chunk_document(
            real_document,
            tokenizer=word_tokenizer,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        document_words = real_document.text.split()
        assert chunks[0].text.split()[0] == document_words[0]
        assert chunks[-1].text.split()[-1] == document_words[-1]

    def test_no_chunk_exceeds_the_configured_chunk_size(
        self, real_document, word_tokenizer, settings, real_document_long_form_source_types
    ) -> None:
        chunks = chunk_document(
            real_document,
            tokenizer=word_tokenizer,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert all(chunk.token_count <= settings.chunk_size_tokens for chunk in chunks)

    def test_consecutive_chunks_overlap_by_the_configured_token_count(
        self, real_document, word_tokenizer, settings, real_document_long_form_source_types
    ) -> None:
        chunks = chunk_document(
            real_document,
            tokenizer=word_tokenizer,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        overlap = settings.chunk_overlap_tokens
        assert all(
            first.text.split()[-overlap:] == second.text.split()[:overlap]
            for first, second in zip(chunks, chunks[1:])
        )

    def test_heading_metadata_is_absent_since_structure_is_none(
        self, real_document, word_tokenizer, settings, real_document_long_form_source_types
    ) -> None:
        chunks = chunk_document(
            real_document,
            tokenizer=word_tokenizer,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert all(chunk.metadata["heading"] is None for chunk in chunks)
        assert all(chunk.metadata["title"] == "Alpinist Studios Company Handbook" for chunk in chunks)


class TestFullPipelineRealDocument:
    def test_every_chunk_is_embedded_with_the_configured_dimension(
        self,
        real_document,
        word_tokenizer,
        fake_embedding_model,
        settings,
        real_document_long_form_source_types,
    ) -> None:
        result = process_document(
            real_document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert len(result) > 1
        assert all(len(ec.embedding) == settings.embedding_dimension for ec in result)

    def test_embedding_calls_are_batched_into_one_call(
        self,
        real_document,
        word_tokenizer,
        fake_embedding_model,
        settings,
        real_document_long_form_source_types,
    ) -> None:
        result = process_document(
            real_document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert len(fake_embedding_model.calls) == 1
        assert len(fake_embedding_model.calls[0]["texts"]) == len(result)

    def test_traceability_back_to_source_id_is_preserved(
        self,
        real_document,
        word_tokenizer,
        fake_embedding_model,
        settings,
        real_document_long_form_source_types,
    ) -> None:
        result = process_document(
            real_document,
            settings=settings,
            tokenizer=word_tokenizer,
            embedding_model=fake_embedding_model,
            long_form_source_types=real_document_long_form_source_types,
            structured_source_types=frozenset(),
        )
        assert all(ec.chunk.source_id == "alpinist-studios-handbook" for ec in result)

"""Unit tests for ingestion/chunking.py."""

from __future__ import annotations

import pytest

from ingestion.chunk_embed.chunking import UnknownSourceTypeError, chunk_document
from ingestion.chunk_embed.types import HeadingMarker
from chunk_embed.conftest import make_document


def _chunk(document, tokenizer, long_form_types, structured_types, **overrides):
    kwargs = dict(
        tokenizer=tokenizer,
        chunk_size_tokens=500,
        chunk_overlap_tokens=75,
        long_form_source_types=long_form_types,
        structured_source_types=structured_types,
    )
    kwargs.update(overrides)
    return chunk_document(document, **kwargs)


class TestEmptyAndSmallDocuments:
    def test_empty_document_produces_zero_chunks(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="")
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert chunks == ()

    def test_very_small_document_produces_one_chunk(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="hi")
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].text == "hi"

    def test_document_smaller_than_chunk_size_produces_one_chunk(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(100))  # 100 tokens < 500
        document = make_document(text=text)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].token_count == 100


class TestExactChunkSizeBoundary:
    def test_exactly_chunk_size_tokens_stays_one_chunk(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(500))  # exactly 500 tokens
        document = make_document(text=text)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].token_count == 500

    def test_one_token_over_chunk_size_splits_into_two(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(501))  # 501 tokens
        document = make_document(text=text)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 2


class TestMultipleChunksAndOrdering:
    def test_large_document_produces_multiple_chunks_in_order(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(1200))
        document = make_document(text=text)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) > 1
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_chunks_cover_the_full_text_in_document_order(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        words = [f"word{i}" for i in range(1200)]
        text = " ".join(words)
        document = make_document(text=text)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        # First chunk starts at the first word; last chunk ends at the last word.
        assert chunks[0].text.split()[0] == words[0]
        assert chunks[-1].text.split()[-1] == words[-1]


class TestOverlapCorrectness:
    def test_consecutive_chunks_overlap_by_configured_token_count(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(1000))
        document = make_document(text=text)
        chunks = _chunk(
            document,
            word_tokenizer,
            long_form_source_types,
            structured_source_types,
            chunk_size_tokens=500,
            chunk_overlap_tokens=75,
        )
        assert len(chunks) >= 2
        first_words = chunks[0].text.split()
        second_words = chunks[1].text.split()
        overlap = [w for w in first_words[-75:] if w in second_words[:75]]
        # The last 75 words of chunk 0 should reappear as the first 75 of chunk 1.
        assert first_words[-75:] == second_words[:75]

    def test_zero_overlap_produces_no_shared_tokens_between_chunks(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(1000))
        document = make_document(text=text)
        chunks = _chunk(
            document,
            word_tokenizer,
            long_form_source_types,
            structured_source_types,
            chunk_size_tokens=500,
            chunk_overlap_tokens=0,
        )
        first_words = set(chunks[0].text.split())
        second_words = set(chunks[1].text.split())
        assert first_words.isdisjoint(second_words)


class TestUnicodeAndNewlines:
    def test_unicode_text_is_preserved(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="héllo wörld 你好 emoji😀test")
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].text == "héllo wörld 你好 emoji😀test"

    def test_newlines_within_a_small_document_are_preserved(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="line one\nline two\nline three")
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].text == "line one\nline two\nline three"


class TestStructureAwareSplitting:
    def test_heading_is_included_in_the_section_that_follows_it(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = "Intro words here. ### Liability Coverage clause details follow."
        position = text.index("### Liability Coverage")
        structure = (HeadingMarker(heading_text="### Liability Coverage", level=3, position=position),)
        document = make_document(text=text, structure=structure, metadata={"title": "Policy"})

        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)

        assert len(chunks) == 2
        assert chunks[0].text == text[:position]
        assert chunks[1].text.startswith("### Liability Coverage")

    def test_preamble_and_heading_metadata(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = "Intro words here. ### Liability Coverage clause details follow."
        position = text.index("### Liability Coverage")
        structure = (HeadingMarker(heading_text="### Liability Coverage", level=3, position=position),)
        document = make_document(text=text, structure=structure, metadata={"title": "Policy"})

        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)

        assert chunks[0].metadata["heading"] is None
        assert chunks[0].metadata["heading_level"] is None
        assert chunks[1].metadata["heading"] == "### Liability Coverage"
        assert chunks[1].metadata["heading_level"] == 3
        assert chunks[0].metadata["title"] == "Policy"
        assert chunks[1].metadata["title"] == "Policy"

    def test_structure_starting_at_position_zero_has_no_preamble_section(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = "### First Heading content right after."
        structure = (HeadingMarker(heading_text="### First Heading", level=3, position=0),)
        document = make_document(text=text, structure=structure)

        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)

        assert len(chunks) == 1
        assert chunks[0].metadata["heading"] == "### First Heading"

    def test_multiple_headings_produce_multiple_sections(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = "## A section text. ## B section text. ## C section text."
        pos_a = text.index("## A")
        pos_b = text.index("## B")
        pos_c = text.index("## C")
        structure = (
            HeadingMarker(heading_text="## A", level=2, position=pos_a),
            HeadingMarker(heading_text="## B", level=2, position=pos_b),
            HeadingMarker(heading_text="## C", level=2, position=pos_c),
        )
        document = make_document(text=text, structure=structure)

        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)

        assert len(chunks) == 3
        assert [c.metadata["heading"] for c in chunks] == ["## A", "## B", "## C"]

    def test_structure_none_falls_through_to_recursive_splitting_only(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(50))
        document = make_document(text=text, structure=None)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].metadata["heading"] is None


class TestStructuredSourceType:
    def test_one_chunk_no_splitting_regardless_of_size(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"field{i}" for i in range(900))  # far over chunk_size_tokens
        document = make_document(source_type="csv", text=text, structure=None)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert len(chunks) == 1
        assert chunks[0].text == text

    def test_empty_structured_document_produces_zero_chunks(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(source_type="csv", text="", structure=None)
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert chunks == ()


class TestInvalidSourceType:
    def test_unrecognized_source_type_raises(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(source_type="mystery_type", text="some text")
        with pytest.raises(UnknownSourceTypeError):
            _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)

    def test_source_type_in_both_sets_raises(
        self, word_tokenizer
    ) -> None:
        ambiguous = frozenset({"weird"})
        document = make_document(source_type="weird", text="some text")
        with pytest.raises(UnknownSourceTypeError):
            _chunk(document, word_tokenizer, ambiguous, ambiguous)


class TestPageRangeResolver:
    def test_omitted_when_no_resolver_given(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(text="hello world")
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert "page_start" not in chunks[0].metadata
        assert "page_end" not in chunks[0].metadata

    def test_injected_resolver_result_merged_into_metadata(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        def resolver(start: int, end: int) -> dict[str, object]:
            return {"page_start": start, "page_end": end}

        document = make_document(text="hello world")
        chunks = _chunk(
            document,
            word_tokenizer,
            long_form_source_types,
            structured_source_types,
            resolve_page_range=resolver,
        )
        assert chunks[0].metadata["page_start"] == 0
        assert chunks[0].metadata["page_end"] == len("hello world")

    def test_resolver_called_with_parent_section_span_for_windowed_chunks(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        text = " ".join(f"word{i}" for i in range(1000))
        calls: list[tuple[int, int]] = []

        def resolver(start: int, end: int) -> dict[str, object]:
            calls.append((start, end))
            return {}

        document = make_document(text=text)
        chunks = _chunk(
            document,
            word_tokenizer,
            long_form_source_types,
            structured_source_types,
            resolve_page_range=resolver,
        )
        # Every windowed chunk from the single section resolves against
        # that same section's full span (documented limitation).
        assert len(calls) == len(chunks)
        assert all(call == (0, len(text)) for call in calls)


class TestMetadataDoesNotDuplicateSourceId:
    def test_source_id_only_in_dedicated_field(
        self, word_tokenizer, long_form_source_types, structured_source_types
    ) -> None:
        document = make_document(source_id="doc-42", text="hello world")
        chunks = _chunk(document, word_tokenizer, long_form_source_types, structured_source_types)
        assert chunks[0].source_id == "doc-42"
        assert "source_id" not in chunks[0].metadata


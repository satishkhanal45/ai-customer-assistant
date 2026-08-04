"""
Demo script: shows an input ExtractedDocument and the chunks it produces.

This is NOT part of the pytest unit test suite (unit tests assert
correctness silently; this script is purely for visual inspection —
e.g. to eyeball chunk boundaries, overlap, and metadata by hand).

Uses a fake in-memory tokenizer, same as the unit tests, so this runs
instantly with no model download and no network access. If you want to
see output from the REAL BGE tokenizer instead, see the
"real tokenizer" variant noted at the bottom of this file.

Run from the backend/ directory:
    uv run python scripts/demo_chunking.py
"""

from __future__ import annotations

from ingestion.chunk_embed.chunking import chunk_document
from ingestion.chunk_embed.types import ExtractedDocument, HeadingMarker


class DemoWordTokenizer:
    """
    Same whitespace-based fake tokenizer used in the unit tests
    (see tests/ingestion/conftest.py:WordTokenizer) — reproduced here so
    this script has no dependency on the test package.
    """

    def __init__(self) -> None:
        self._id_to_word: list[str] = []
        self._word_to_id: dict[str, int] = {}

    def _id_for(self, word: str) -> int:
        if word not in self._word_to_id:
            self._word_to_id[word] = len(self._id_to_word)
            self._id_to_word.append(word)
        return self._word_to_id[word]

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        return [self._id_for(word) for word in text.split()]

    def decode(self, token_ids: list[int], skip_special_tokens: bool = True) -> str:
        return " ".join(self._id_to_word[token_id] for token_id in token_ids)


def _print_document(document: ExtractedDocument) -> None:
    print("=" * 80)
    print("INPUT DOCUMENT")
    print("=" * 80)
    print(f"source_id:   {document.source_id}")
    print(f"source_type: {document.source_type}")
    print(f"metadata:    {dict(document.metadata)}")
    print(f"structure:   {document.structure}")
    print(f"text ({len(document.text)} chars):")
    print(f"  {document.text!r}")
    print()


def _print_chunks(chunks: tuple) -> None:
    print("=" * 80)
    print(f"OUTPUT: {len(chunks)} chunk(s)")
    print("=" * 80)
    for chunk in chunks:
        print(f"[chunk_index={chunk.chunk_index}] token_count={chunk.token_count}")
        print(f"  source_id: {chunk.source_id}")
        print(f"  metadata:  {dict(chunk.metadata)}")
        print(f"  text:      {chunk.text!r}")
        print()


def demo_long_form_with_structure() -> None:
    text = (
        "This policy provides general information for all customers. "
        "### Liability Coverage "
        "This section explains what liability coverage protects against, "
        "including third-party claims and legal defense costs. "
        "### Exclusions "
        "This section lists what is not covered under this policy, "
        "such as intentional damage or acts of war."
    )
    liability_position = text.index("### Liability Coverage")
    exclusions_position = text.index("### Exclusions")

    document = ExtractedDocument(
        source_id="demo-doc-1",
        source_type="markdown",
        text=text,
        structure=(
            HeadingMarker(heading_text="### Liability Coverage", level=3, position=liability_position),
            HeadingMarker(heading_text="### Exclusions", level=3, position=exclusions_position),
        ),
        metadata={"title": "Sample Insurance Policy"},
    )

    _print_document(document)

    chunks = chunk_document(
        document,
        tokenizer=DemoWordTokenizer(),
        chunk_size_tokens=500,
        chunk_overlap_tokens=75,
        long_form_source_types=frozenset({"markdown"}),
        structured_source_types=frozenset({"csv"}),
    )

    _print_chunks(chunks)


def demo_long_form_recursive_split() -> None:
    # A single section with no structure, long enough to force recursive
    # token-window splitting (chunk_size_tokens=20, overlap=5, for a
    # readable demo instead of the real 500/75 configuration).
    text = " ".join(f"word{i}" for i in range(50))

    document = ExtractedDocument(
        source_id="demo-doc-2",
        source_type="markdown",
        text=text,
        structure=None,
        metadata={},
    )

    _print_document(document)

    chunks = chunk_document(
        document,
        tokenizer=DemoWordTokenizer(),
        chunk_size_tokens=20,
        chunk_overlap_tokens=5,
        long_form_source_types=frozenset({"markdown"}),
        structured_source_types=frozenset({"csv"}),
    )

    _print_chunks(chunks)


def demo_structured_no_splitting() -> None:
    document = ExtractedDocument(
        source_id="demo-doc-3",
        source_type="csv",
        text="policy_id,customer_name,premium\nP-001,Jane Doe,450.00",
        structure=None,
        metadata={},
    )

    _print_document(document)

    chunks = chunk_document(
        document,
        tokenizer=DemoWordTokenizer(),
        chunk_size_tokens=500,
        chunk_overlap_tokens=75,
        long_form_source_types=frozenset({"markdown"}),
        structured_source_types=frozenset({"csv"}),
    )

    _print_chunks(chunks)


if __name__ == "__main__":
    print("\n### DEMO 1: long-form document, structure-aware splitting ###\n")
    demo_long_form_with_structure()

    print("\n### DEMO 2: long-form document, recursive token-window splitting ###\n")
    demo_long_form_recursive_split()

    print("\n### DEMO 3: structured document, no splitting ###\n")
    demo_structured_no_splitting()

# --------------------------------------------------------------------------
# To see output from the REAL BGE tokenizer instead of DemoWordTokenizer,
# replace DemoWordTokenizer() with:
#
#   from ingestion.tokenizer import get_tokenizer
#   get_tokenizer("BAAI/bge-base-en-v1.5")
#
# This downloads the real tokenizer on first run (network required) and
# produces real BGE token counts/boundaries instead of whitespace-based
# approximations.
# --------------------------------------------------------------------------


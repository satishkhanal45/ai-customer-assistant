"""
Demo script: runs the real Alpinist Studios handbook text through
chunk_document and prints every resulting chunk for visual inspection.

Same purpose as demo_chunking.py (NOT part of the pytest suite — for
eyeballing chunk boundaries/overlap/metadata by hand) but against a real
~5,900-word document instead of hand-written synthetic strings.

Uses the same in-memory DemoWordTokenizer as demo_chunking.py, so this
runs instantly with no model download and no network access.

Run from the backend/ directory:
    uv run python scripts/demo_real_document.py
"""

from __future__ import annotations

from pathlib import Path

from ingestion.chunk_embed.chunking import chunk_document
from ingestion.chunk_embed.types import ExtractedDocument

_FIXTURE_PATH = (
    Path(__file__).parent.parent / "tests" / "chunk_embed" / "fixtures" / "alpinist_studios_handbook.txt"
)


class DemoWordTokenizer:
    """Same whitespace-based fake tokenizer used in demo_chunking.py."""

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
    print(f"text ({len(document.text)} chars, {len(document.text.split())} words)")
    print()


def _print_chunks(chunks: tuple) -> None:
    print("=" * 80)
    print(f"OUTPUT: {len(chunks)} chunk(s)")
    print("=" * 80)
    for chunk in chunks:
        preview = chunk.text[:200].replace("\n", " ")
        print(f"[chunk_index={chunk.chunk_index}] token_count={chunk.token_count}")
        print(f"  source_id: {chunk.source_id}")
        print(f"  metadata:  {dict(chunk.metadata)}")
        print(f"  text[:200]: {preview!r}...")
        print()


def demo_real_document() -> None:
    text = _FIXTURE_PATH.read_text(encoding="utf-8")

    document = ExtractedDocument(
        source_id="alpinist-studios-handbook",
        source_type="docx",
        text=text,
        structure=None,
        metadata={"title": "Alpinist Studios Company Handbook"},
    )

    _print_document(document)

    chunks = chunk_document(
        document,
        tokenizer=DemoWordTokenizer(),
        chunk_size_tokens=500,
        chunk_overlap_tokens=75,
        long_form_source_types=frozenset({"docx"}),
        structured_source_types=frozenset(),
    )

    _print_chunks(chunks)


if __name__ == "__main__":
    demo_real_document()

# """
# Demo script: runs the real Alpinist Studios handbook text through
# chunk_document and prints every resulting chunk for visual inspection.

# Same purpose as demo_chunking.py (NOT part of the pytest suite — for
# eyeballing chunk boundaries/overlap/metadata by hand) but against a real
# ~5,900-word document instead of hand-written synthetic strings.

# Uses the same in-memory DemoWordTokenizer as demo_chunking.py, so this
# runs instantly with no model download and no network access.

# Run from the backend/ directory:
#     uv run python scripts/demo_real_document.py
# """

# from __future__ import annotations

# from pathlib import Path

# from ingestion.chunk_embed.chunking import chunk_document
# from ingestion.chunk_embed.types import ExtractedDocument

# _FIXTURE_PATH = (
#     Path(__file__).parent.parent / "tests" / "chunk_embed" / "fixtures" / "alpinist_studios_handbook.txt"
# )


# class DemoWordTokenizer:
#     """Same whitespace-based fake tokenizer used in demo_chunking.py."""

#     def __init__(self) -> None:
#         self._id_to_word: list[str] = []
#         self._word_to_id: dict[str, int] = {}

#     def _id_for(self, word: str) -> int:
#         if word not in self._word_to_id:
#             self._word_to_id[word] = len(self._id_to_word)
#             self._id_to_word.append(word)
#         return self._word_to_id[word]

#     def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
#         return [self._id_for(word) for word in text.split()]

#     def decode(self, token_ids: list[int], skip_special_tokens: bool = True) -> str:
#         return " ".join(self._id_to_word[token_id] for token_id in token_ids)


# def _print_document(document: ExtractedDocument) -> None:
#     print("=" * 80)
#     print("INPUT DOCUMENT")
#     print("=" * 80)
#     print(f"source_id:   {document.source_id}")
#     print(f"source_type: {document.source_type}")
#     print(f"metadata:    {dict(document.metadata)}")
#     print(f"structure:   {document.structure}")
#     print(f"text ({len(document.text)} chars, {len(document.text.split())} words)")
#     print()


# def _print_chunks(chunks: tuple) -> None:
#     print("=" * 80)
#     print(f"OUTPUT: {len(chunks)} chunk(s)")
#     print("=" * 80)
#     for chunk in chunks:
#         preview = chunk.text[:200].replace("\n", " ")
#         print(f"[chunk_index={chunk.chunk_index}] token_count={chunk.token_count}")
#         print(f"  source_id: {chunk.source_id}")
#         print(f"  metadata:  {dict(chunk.metadata)}")
#         print(f"  text[:200]: {preview!r}...")
#         print()


# def demo_real_document() -> None:
#     text = _FIXTURE_PATH.read_text(encoding="utf-8")

#     document = ExtractedDocument(
#         source_id="alpinist-studios-handbook",
#         source_type="docx",
#         text=text,
#         structure=None,
#         metadata={"title": "Alpinist Studios Company Handbook"},
#     )

#     _print_document(document)

#     chunks = chunk_document(
#         document,
#         tokenizer=DemoWordTokenizer(),
#         chunk_size_tokens=500,
#         chunk_overlap_tokens=75,
#         long_form_source_types=frozenset({"docx"}),
#         structured_source_types=frozenset(),
#     )

#     _print_chunks(chunks)


# if __name__ == "__main__":
#     demo_real_document()



"""
Demo script: runs the REAL Alpinist Studios handbook through the full
pipeline (chunking -> embedding) and prints both stages to the terminal.

This is the embedding-inclusive sibling of demo_chunking.py — same
"visual inspection, not pytest assertions" purpose, same offline-safe
test doubles (no network, no model download), just carried one stage
further so you can eyeball the embedding vectors alongside the chunks
they came from.

Run from the backend/ directory (adjust --fixture if your layout differs):
    uv run python scripts/demo_real_document.py
    uv run python scripts/demo_real_document.py --fixture path/to/handbook.txt --preview-dims 8
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from ingestion.chunk_embed.config import IngestionSettings
from ingestion.chunk_embed.pipeline import process_document
from ingestion.chunk_embed.types import EmbeddedChunk, ExtractedDocument

_DEFAULT_FIXTURE_CANDIDATES = (
    Path("tests/chunk_embed/fixtures/alpinist_studios_handbook.txt"),
    Path("backend/tests/chunk_embed/fixtures/alpinist_studios_handbook.txt"),
)


class DemoWordTokenizer:
    """Same whitespace-based fake tokenizer used in demo_chunking.py / conftest.py."""

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


class DemoFakeEmbeddingModel:
    """
    Deterministic, network-free stand-in for SentenceTransformer.encode().

    Each text is hashed (SHA-256) into `dimension` pseudo-random-but-
    reproducible floats in [-1, 1] — same input text always yields the
    same vector, so overlapping chunks visibly share partial similarity
    in the printed preview without ever touching the real BGE model.
    """

    def __init__(self, dimension: int = 768) -> None:
        self.dimension = dimension

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] for i in range(self.dimension)]
        return [(byte / 127.5) - 1.0 for byte in raw]


def _load_document(fixture_path: Path) -> ExtractedDocument:
    text = fixture_path.read_text(encoding="utf-8")
    return ExtractedDocument(
        source_id="alpinist-studios-handbook",
        source_type="docx",
        text=text,
        structure=None,
        metadata={"title": "Alpinist Studios Company Handbook"},
    )


def _resolve_fixture_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    existing = next((p for p in _DEFAULT_FIXTURE_CANDIDATES if p.exists()), None)
    if existing is None:
        raise FileNotFoundError(
            "Could not find alpinist_studios_handbook.txt in the default "
            f"locations {_DEFAULT_FIXTURE_CANDIDATES}. Pass --fixture explicitly."
        )
    return existing


def _print_document(document: ExtractedDocument) -> None:
    print("=" * 80)
    print("INPUT DOCUMENT")
    print("=" * 80)
    print(f"source_id:   {document.source_id}")
    print(f"source_type: {document.source_type}")
    print(f"metadata:    {dict(document.metadata)}")
    print(f"text:        {len(document.text)} chars, {len(document.text.split())} words")
    print()


def _print_embedded_chunk(embedded: EmbeddedChunk, preview_dims: int) -> None:
    chunk = embedded.chunk
    preview = ", ".join(f"{v:.4f}" for v in embedded.embedding[:preview_dims])
    print(f"[chunk_index={chunk.chunk_index}] token_count={chunk.token_count}")
    print(f"  metadata:        {dict(chunk.metadata)}")
    print(f"  text (excerpt):  {chunk.text[:90]!r}...")
    print(f"  embedding dim:   {len(embedded.embedding)}")
    print(f"  embedding[:{preview_dims}]: [{preview}]")
    print()


def _print_embedded_chunks(embedded_chunks: tuple[EmbeddedChunk, ...], preview_dims: int) -> None:
    print("=" * 80)
    print(f"OUTPUT: {len(embedded_chunks)} embedded chunk(s)")
    print("=" * 80)
    for embedded in embedded_chunks:
        _print_embedded_chunk(embedded, preview_dims)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to the real document fixture text file.",
    )
    parser.add_argument(
        "--preview-dims",
        type=int,
        default=6,
        help="How many leading embedding dimensions to print per chunk.",
    )
    args = parser.parse_args()

    fixture_path = _resolve_fixture_path(args.fixture)
    document = _load_document(fixture_path)
    settings = IngestionSettings(_env_file=None)

    _print_document(document)

    embedded_chunks = process_document(
        document,
        settings=settings,
        tokenizer=DemoWordTokenizer(),
        embedding_model=DemoFakeEmbeddingModel(dimension=settings.embedding_dimension),
        long_form_source_types=frozenset({"docx"}),
        structured_source_types=frozenset(),
    )

    _print_embedded_chunks(embedded_chunks, args.preview_dims)


if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------
# To see output from the REAL BGE tokenizer + embedding model instead of
# the deterministic doubles above, swap in:
#
#   from ingestion.chunk_embed.tokenizer import get_tokenizer
#   from ingestion.chunk_embed.embedding import get_embedding_model
#
#   tokenizer=get_tokenizer(settings.embedding_model_name)
#   embedding_model=get_embedding_model(settings.embedding_model_name)
#
# This downloads both on first run (network required) and produces real
# BGE token counts and 768-dim normalized vectors instead of the fake
# hash-based ones used here.
# --------------------------------------------------------------------------
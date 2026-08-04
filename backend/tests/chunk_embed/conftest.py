"""
Shared fixtures for the ingestion module's test suite.

No real tokenizer or embedding model is ever loaded in tests: WordTokenizer
and FakeEmbeddingModel below are deterministic test doubles that satisfy
the same call interface (encode/decode, encode) that tokenizer.py and
embedding.py depend on, without any network access or model download.
"""

from __future__ import annotations

import pytest

from ingestion.chunk_embed.config import IngestionSettings
from ingestion.chunk_embed.types import Chunk, ExtractedDocument, HeadingMarker


class WordTokenizer:
    """
    Deterministic test double for a HuggingFace tokenizer.

    Tokenizes by whitespace-splitting; each unique word gets a stable
    integer id (assigned on first sight), so encode() -> decode() is an
    exact round trip and overlap between token windows can be verified
    precisely by comparing the words they share.
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


class FakeEmbeddingModel:
    """
    Deterministic test double for a SentenceTransformer.

    Records every call (texts + kwargs) so tests can assert on how it was
    invoked (e.g. normalize_embeddings, batch_size), and can be configured
    to raise or to return the wrong dimension, to exercise embedding.py's
    error paths without needing a real model to fail.
    """

    def __init__(self, dimension: int = 768, fail: bool = False, wrong_dimension: bool = False) -> None:
        self.dimension = dimension
        self.fail = fail
        self.wrong_dimension = wrong_dimension
        self.calls: list[dict[str, object]] = []

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "kwargs": kwargs})
        if self.fail:
            raise RuntimeError("simulated embedding backend failure")
        dimension = self.dimension - 1 if self.wrong_dimension else self.dimension
        return [[float(len(text) + i) for i in range(dimension)] for i, text in enumerate(texts)]


@pytest.fixture
def word_tokenizer() -> WordTokenizer:
    """A fresh deterministic tokenizer test double for each test."""
    return WordTokenizer()


@pytest.fixture
def fake_embedding_model() -> FakeEmbeddingModel:
    """A fresh deterministic embedding-model test double for each test."""
    return FakeEmbeddingModel()


@pytest.fixture
def settings() -> IngestionSettings:
    """
    Confirmed pipeline settings, constructed directly (bypassing any real
    .env file on disk) so tests are isolated from the environment they
    happen to run in.
    """
    return IngestionSettings(_env_file=None)


@pytest.fixture
def long_form_source_types() -> frozenset[str]:
    """Arbitrary long-form source_type values, for chunking/pipeline tests."""
    return frozenset({"markdown"})


@pytest.fixture
def structured_source_types() -> frozenset[str]:
    """Arbitrary structured source_type values, for chunking/pipeline tests."""
    return frozenset({"csv"})


def make_document(
    *,
    source_id: str = "doc-1",
    source_type: str = "markdown",
    text: str = "hello world",
    structure: tuple[HeadingMarker, ...] | None = None,
    metadata: dict[str, object] | None = None,
) -> ExtractedDocument:
    """Factory for ExtractedDocument with sensible defaults, for terse tests."""
    return ExtractedDocument(
        source_id=source_id,
        source_type=source_type,
        text=text,
        structure=structure,
        metadata=metadata or {},
    )


def make_chunk(
    *,
    source_id: str = "doc-1",
    chunk_index: int = 0,
    text: str = "hello world",
    token_count: int = 2,
    metadata: dict[str, object] | None = None,
) -> Chunk:
    """Factory for Chunk with sensible defaults, for terse tests."""
    return Chunk(
        source_id=source_id,
        chunk_index=chunk_index,
        text=text,
        token_count=token_count,
        metadata=metadata or {},
    )


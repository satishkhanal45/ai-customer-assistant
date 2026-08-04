"""Unit tests for ingestion/types.py — the immutable data layer."""

from __future__ import annotations

import dataclasses

import pytest

from ingestion.chunk_embed.types import Chunk, EmbeddedChunk, ExtractedDocument, HeadingMarker
from backend.tests.chunk_embed.conftest import make_chunk, make_document


class TestHeadingMarker:
    def test_construction(self) -> None:
        marker = HeadingMarker(heading_text="Intro", level=1, position=0)
        assert marker.heading_text == "Intro"
        assert marker.level == 1
        assert marker.position == 0

    def test_immutable(self) -> None:
        marker = HeadingMarker(heading_text="Intro", level=1, position=0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            marker.level = 2  # type: ignore[misc]


class TestExtractedDocument:
    def test_construction_with_structure(self) -> None:
        structure = (HeadingMarker(heading_text="H1", level=1, position=0),)
        document = make_document(structure=structure, metadata={"title": "Doc"})
        assert document.structure == structure
        assert document.metadata["title"] == "Doc"

    def test_structure_none_is_preserved(self) -> None:
        document = make_document(structure=None)
        assert document.structure is None

    def test_default_metadata_is_empty(self) -> None:
        document = make_document(metadata=None)
        assert dict(document.metadata) == {}

    def test_metadata_is_immutable(self) -> None:
        document = make_document(metadata={"title": "Doc"})
        with pytest.raises(TypeError):
            document.metadata["title"] = "Changed"  # type: ignore[index]

    def test_metadata_mutation_of_source_dict_does_not_affect_document(self) -> None:
        source = {"title": "Original"}
        document = make_document(metadata=source)
        source["title"] = "Mutated"
        assert document.metadata["title"] == "Original"

    def test_document_itself_is_immutable(self) -> None:
        document = make_document()
        with pytest.raises(dataclasses.FrozenInstanceError):
            document.text = "changed"  # type: ignore[misc]


class TestChunk:
    def test_construction(self) -> None:
        chunk = make_chunk(source_id="s1", chunk_index=3, text="abc", token_count=1)
        assert chunk.source_id == "s1"
        assert chunk.chunk_index == 3
        assert chunk.text == "abc"
        assert chunk.token_count == 1

    def test_default_metadata_is_empty(self) -> None:
        chunk = make_chunk(metadata=None)
        assert dict(chunk.metadata) == {}

    def test_metadata_is_immutable(self) -> None:
        chunk = make_chunk(metadata={"heading": "H1"})
        with pytest.raises(TypeError):
            chunk.metadata["heading"] = "Changed"  # type: ignore[index]

    def test_chunk_itself_is_immutable(self) -> None:
        chunk = make_chunk()
        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.chunk_index = 99  # type: ignore[misc]


class TestEmbeddedChunk:
    def test_construction(self) -> None:
        chunk = make_chunk()
        embedding = tuple(float(i) for i in range(768))
        embedded = EmbeddedChunk(chunk=chunk, embedding=embedding)
        assert embedded.chunk is chunk
        assert embedded.embedding == embedding
        assert len(embedded.embedding) == 768

    def test_immutable(self) -> None:
        chunk = make_chunk()
        embedded = EmbeddedChunk(chunk=chunk, embedding=(0.0,) * 768)
        with pytest.raises(dataclasses.FrozenInstanceError):
            embedded.embedding = (1.0,) * 768  # type: ignore[misc]

    def test_embedding_is_a_tuple_not_a_list(self) -> None:
        chunk = make_chunk()
        embedded = EmbeddedChunk(chunk=chunk, embedding=(0.0,) * 768)
        assert isinstance(embedded.embedding, tuple)


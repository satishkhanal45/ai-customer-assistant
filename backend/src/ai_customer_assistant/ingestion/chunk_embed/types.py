"""
Data layer for the chunking / embedding pipeline.

This module contains ONLY data definitions — no business logic, no I/O,
no side effects. Every structure here is immutable (frozen dataclasses,
tuples instead of lists, MappingProxyType instead of dict) so that values
can be passed freely through pure functions without any risk of one stage
mutating data another stage still depends on.

Contract boundaries:
    ExtractedDocument  -> input to this module (produced upstream, outside
                           the responsibility of this codebase).
    HeadingMarker       -> one entry in ExtractedDocument.structure.
    Chunk               -> output of the chunking stage.
    EmbeddedChunk       -> output of the embedding stage (Chunk + vector).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


def _freeze_mapping(data: Mapping[str, object]) -> MappingProxyType:
    """Return an immutable view over ``data`` without mutating the input."""
    return MappingProxyType(dict(data))


@dataclass(frozen=True, slots=True)
class HeadingMarker:
    """
    A single structural marker extracted from a long-form document
    (e.g. a Markdown heading, or a heading style detected in a DOCX/PDF).

    Attributes:
        heading_text: The literal text of the heading.
        level: Heading depth as an integer (e.g. 1 for H1, 2 for H2, ...).
        position: Character offset of the heading's start within
                  ExtractedDocument.text.
    """

    heading_text: str
    level: int
    position: int


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """
    Input contract for this module. Produced upstream (extraction stage,
    outside this module's responsibility) and consumed by the chunker.

    Attributes:
        source_id: Identifier tying every resulting chunk back to the
                   originating Knowledge Source row.
        source_type: Discriminator used to select structural splitting
                     rules — e.g. "markdown", "docx", "pdf", "csv", "faq".
        text: The full extracted text content.
        structure: Ordered structural markers for long-form documents, or
                   None when the extractor could not determine structure
                   (in which case chunking falls through directly to
                   recursive/size-based splitting).
        metadata: Arbitrary per-document metadata (e.g. page count),
                  stored as an immutable mapping.
    """

    source_id: str
    source_type: str
    text: str
    structure: tuple[HeadingMarker, ...] | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # dataclasses with frozen=True require object.__setattr__ to
        # normalize fields during construction without violating immutability
        # after the instance exists.
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class Chunk:
    """
    A single chunk produced by the chunking stage, prior to embedding.

    Attributes:
        source_id: Inherited from the originating ExtractedDocument.
        chunk_index: Ordinal position of this chunk within its document
                     (0-based, in document order).
        text: The chunk's text content.
        token_count: Number of tokens in ``text``, as measured by the
                     BGE tokenizer (see tokenizer.py).
        metadata: Per-chunk metadata (e.g. page number, row index),
                  stored as an immutable mapping.
    """

    source_id: str
    chunk_index: int
    text: str
    token_count: int
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    """
    A Chunk together with its generated embedding vector.

    Attributes:
        chunk: The source Chunk this embedding was generated from.
        embedding: The embedding vector, normalized, as an immutable tuple
                   of floats (length must equal the configured embedding
                   dimension — validated by the embedding stage, not here).
    """

    chunk: Chunk
    embedding: tuple[float, ...]


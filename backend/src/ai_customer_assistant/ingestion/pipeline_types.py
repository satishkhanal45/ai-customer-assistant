"""
Immutable data types shared across the ingestion pipeline.

This module is the *data layer*: frozen dataclasses and enums only.
No business logic, no I/O. Every other ingestion module imports its
shapes from here rather than redefining ad-hoc dicts/tuples.

Mirrors schema.md (v4) and ingestion_flow.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID


class SourceType(str, Enum):
    FILE_UPLOAD = "FILE_UPLOAD"
    EXTERNAL_INTEGRATION = "EXTERNAL_INTEGRATION"


class FileType(str, Enum):
    PDF = "PDF"
    DOCX = "DOCX"
    MD = "MD"


class VersionStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    STALE = "STALE"
    ARCHIVED = "ARCHIVED"


class JobType(str, Enum):
    INITIAL_INGEST = "INITIAL_INGEST"
    REINDEX = "REINDEX"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class EntityMapRelationship(str, Enum):
    PRIMARY_DOCUMENT = "PRIMARY_DOCUMENT"
    DERIVED_SECTION = "DERIVED_SECTION"
    DERIVED_CHUNK = "DERIVED_CHUNK"
    SUPPLEMENTARY = "SUPPLEMENTARY"


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: UUID
    category_id: UUID | None
    source_name: str
    source_type: SourceType
    origin_system: str | None
    external_reference_id: str | None
    uploaded_by: UUID
    current_version_id: UUID | None


@dataclass(frozen=True, slots=True)
class VersionRef:
    version_id: UUID
    source_id: UUID
    version_number: int
    file_type: FileType | None
    storage_uri: str
    checksum: str
    mime_type: str
    file_size_bytes: int
    status: VersionStatus
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobRef:
    job_id: UUID
    source_id: UUID
    version_id: UUID | None
    job_type: JobType
    status: JobStatus
    triggered_by: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Output of the Tika extraction stage."""

    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """A chunk produced by chunk_embed, not yet persisted."""

    chunk_index: int
    chunk_text: str
    checksum: str
    token_count: int
    page: int | None
    embedding: tuple[float, ...] | None  # None => needs computing


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    """One EAV fact produced by the extraction agent for a single chunk."""

    entity_type: str
    entity_name: str
    namespace: str
    attribute_name: str
    value: str
    value_type: str = "string"
    multivalue: bool = False
    searchable: bool = True


@dataclass(frozen=True, slots=True)
class ExtractedRelation:
    source_entity_type: str
    source_entity_name: str
    target_entity_type: str
    target_entity_name: str
    relation_type: str


@dataclass(frozen=True, slots=True)
class ChunkExtraction:
    """Everything extraction produced for one chunk."""

    chunk_index: int
    entity: tuple[str, str] | None  # (entity_type, name) resolved primary entity, if any
    facts: tuple[ExtractedFact, ...] = ()
    relations: tuple[ExtractedRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionContext:
    """
    The single value threaded through every pipeline stage.

    Each stage function takes an IngestionContext and returns a new one
    (or a Result wrapping one) -- nothing here is ever mutated in place.
    """

    job: JobRef
    source: SourceRef
    version: VersionRef
    raw_bytes: bytes | None = None
    content_type: str | None = None
    extracted: ExtractedDocument | None = None
    # checksum -> embedding, for the previous version's chunks (ingestion_flow.md
    # step 4's reuse match). A plain dict rather than frozenset[str] so the
    # matched embedding can actually be copied forward, not just detected.
    reuse_embeddings: dict[str, tuple[float, ...]] = field(default_factory=dict)
    chunks: tuple[ChunkDraft, ...] = ()
    chunk_extractions: tuple[ChunkExtraction, ...] = ()
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Final report handed back to the queue layer (step 8)."""

    job_id: UUID
    version_id: UUID
    status: JobStatus
    chunks_created_count: int = 0
    entities_created_count: int = 0
    error_details: str | None = None

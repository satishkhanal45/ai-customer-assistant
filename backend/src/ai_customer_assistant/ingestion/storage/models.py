"""SQLAlchemy models for the tables the storage layer reads/writes.

NOTE: in the real codebase these declarative models almost certainly
already exist elsewhere (e.g. ``db.models``), defined
against the full schema in ``schema.md``. They are reproduced here,
scoped to only the columns ``repository.py`` touches, purely so this
module is self-contained and its tests don't depend on an external models
module that isn't part of this deliverable. Replace this import in
``repository.py`` with the project's real models module before merging.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Generic (dialect-agnostic) UUID/JSON types: render as native UUID/JSONB
# on Postgres, and as portable fallbacks (e.g. CHAR32 / TEXT) on sqlite —
# which is what lets this stub double as an in-memory test fixture without
# needing a real Postgres instance.
PG_UUID = Uuid
JSONB = JSON


class Base(DeclarativeBase):
    pass


class KnowledgeSource(Base):
    __tablename__ = "knowledge_source"

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # FILE_UPLOAD / EXTERNAL_INTEGRATION
    origin_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_reference_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source_version.version_id"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeSourceVersion(Base):
    __tablename__ = "knowledge_source_version"

    version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_source.source_id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # PDF / DOCX / MD
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # PENDING/.../FAILED/STALE
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reindexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KnowledgeInjectionJob(Base):
    __tablename__ = "knowledge_injection_job"

    job_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_source.source_id"), nullable=False
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_source_version.version_id"), nullable=True
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    chunks_created_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entities_created_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

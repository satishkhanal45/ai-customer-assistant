"""
All ORM models for the knowledge base schema, in one file.

Table order below follows dependency order (a table only references ones
defined above it), except for the one genuine cycle: knowledge_source and
knowledge_source_version reference each other. That's resolved with
use_alter=True on knowledge_source.current_version_id (see the comment
on that column) rather than by reordering — a true cycle can't be solved
by reordering alone.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for every ORM model in the knowledge base."""


# ---------------------------------------------------------------------------
# Core entity-attribute-value model
# ---------------------------------------------------------------------------


class Entity(Base):
    __tablename__ = "entity"
    __table_args__ = (UniqueConstraint("entity_type", "name", name="uq_entity_type_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    values: Mapped[list["Value"]] = relationship(back_populates="entity")
    outgoing_relations: Mapped[list["Relation"]] = relationship(
        foreign_keys="Relation.source_entity_id", back_populates="source_entity"
    )
    incoming_relations: Mapped[list["Relation"]] = relationship(
        foreign_keys="Relation.target_entity_id", back_populates="target_entity"
    )
    embedding_chunks: Mapped[list["EmbeddingChunk"]] = relationship(back_populates="entity")


VALUE_TYPES = ("string", "number", "boolean", "date", "json")


class Attribute(Base):
    __tablename__ = "attribute"
    __table_args__ = (
        UniqueConstraint("namespace", "name", name="uq_attribute_namespace_name"),
        CheckConstraint(f"value_type IN {VALUE_TYPES!r}", name="ck_attribute_value_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value_type: Mapped[str] = mapped_column(String(64), nullable=False, server_default="string")
    multivalue: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    values: Mapped[list["Value"]] = relationship(back_populates="attribute")


class Value(Base):
    __tablename__ = "value"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attribute_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attribute.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value: Mapped[str] = mapped_column(Text, nullable=False)
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    entity: Mapped["Entity"] = relationship(back_populates="values")
    attribute: Mapped["Attribute"] = relationship(back_populates="values")


class Relation(Base):
    __tablename__ = "relation"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    source_entity: Mapped["Entity"] = relationship(
        foreign_keys=[source_entity_id], back_populates="outgoing_relations"
    )
    target_entity: Mapped["Entity"] = relationship(
        foreign_keys=[target_entity_id], back_populates="incoming_relations"
    )


# ---------------------------------------------------------------------------
# Users (stub) and knowledge categories
# ---------------------------------------------------------------------------


class AppUser(Base):
    """
    ASSUMPTION: minimal stand-in for the `user` table referenced by
    knowledge_source.uploaded_by, which the schema doc mentions but does
    not define. Swap this for your real user/auth model if one already
    exists.
    """

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_service_account: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class KnowledgeCategory(Base):
    __tablename__ = "knowledge_category"

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    parent_category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_category.category_id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    sources: Mapped[list["KnowledgeSource"]] = relationship(back_populates="category")


# ---------------------------------------------------------------------------
# Knowledge source / version (the v3 split) and everything derived from it
# ---------------------------------------------------------------------------

SourceTypeEnum = SAEnum("FILE_UPLOAD", "EXTERNAL_INTEGRATION", name="knowledge_source_type")
FileTypeEnum = SAEnum("PDF", "DOCX", "MD", name="knowledge_file_type")
VersionStatusEnum = SAEnum(
    "PENDING", "PROCESSING", "INDEXED", "FAILED", "STALE", "ARCHIVED",
    name="knowledge_version_status",
)
RelationshipTypeEnum = SAEnum(
    "PRIMARY_DOCUMENT", "DERIVED_SECTION", "DERIVED_CHUNK", "SUPPLEMENTARY",
    name="knowledge_relationship_type",
)
JobTypeEnum = SAEnum("INITIAL_INGEST", "REINDEX", "UPDATE", "DELETE", name="knowledge_job_type")
JobStatusEnum = SAEnum("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", name="knowledge_job_status")


class KnowledgeSource(Base):
    __tablename__ = "knowledge_source"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_category.category_id"), nullable=True
    )
    source_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(SourceTypeEnum, nullable=False)
    origin_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_reference_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True)
    # use_alter=True: knowledge_source <-> knowledge_source_version reference each
    # other (a genuine cycle). This tells SQLAlchemy/Alembic to create both tables
    # first and add this particular FK afterwards via ALTER TABLE.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "knowledge_source_version.version_id",
            use_alter=True,
            name="fk_knowledge_source_current_version",
        ),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    category: Mapped["KnowledgeCategory | None"] = relationship(back_populates="sources")
    versions: Mapped[list["KnowledgeSourceVersion"]] = relationship(
        back_populates="source", foreign_keys="KnowledgeSourceVersion.source_id"
    )
    current_version: Mapped["KnowledgeSourceVersion | None"] = relationship(
        foreign_keys=[current_version_id], viewonly=True
    )
    jobs: Mapped[list["KnowledgeInjectionJob"]] = relationship(back_populates="source")


class KnowledgeSourceVersion(Base):
    __tablename__ = "knowledge_source_version"
    __table_args__ = (
        UniqueConstraint("source_id", "version_number", name="uq_knowledge_source_version_source_version"),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_source.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_type: Mapped[str | None] = mapped_column(FileTypeEnum, nullable=True)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(VersionStatusEnum, nullable=False, server_default="PENDING")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    last_ingested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_reindexed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    source: Mapped["KnowledgeSource"] = relationship(back_populates="versions", foreign_keys=[source_id])
    chunks: Mapped[list["EmbeddingChunk"]] = relationship(back_populates="version")
    entity_map_entries: Mapped[list["KnowledgeSourceEntityMap"]] = relationship(back_populates="version")
    jobs: Mapped[list["KnowledgeInjectionJob"]] = relationship(back_populates="version")


class EmbeddingChunk(Base):
    __tablename__ = "embedding_chunk"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_source_version.version_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("entity.id"), nullable=True, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    version: Mapped["KnowledgeSourceVersion"] = relationship(back_populates="chunks")
    entity: Mapped["Entity | None"] = relationship(back_populates="embedding_chunks")


class KnowledgeSourceEntityMap(Base):
    __tablename__ = "knowledge_source_entity_map"

    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_source_version.version_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(RelationshipTypeEnum, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    version: Mapped["KnowledgeSourceVersion"] = relationship(back_populates="entity_map_entries")


class KnowledgeInjectionJob(Base):
    __tablename__ = "knowledge_injection_job"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_source.source_id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_source_version.version_id"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(JobTypeEnum, nullable=False)
    status: Mapped[str] = mapped_column(JobStatusEnum, nullable=False, server_default="QUEUED")
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    chunks_created_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entities_created_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped["KnowledgeSource"] = relationship(back_populates="jobs")
    version: Mapped["KnowledgeSourceVersion | None"] = relationship(back_populates="jobs")

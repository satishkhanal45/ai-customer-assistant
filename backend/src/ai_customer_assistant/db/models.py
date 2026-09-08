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
    JSON,
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

    __table_args__ = (
        UniqueConstraint("entity_id", "attribute_id", "value", name="uq_value_entity_attribute_value"),
    )

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

    __table_args__ = (
        UniqueConstraint(
            "source_entity_id", "target_entity_id", "relation_type", name="uq_relation_source_target_type"
        ),
    )

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
    """A person (or service account) who can be attributed work.

    Originally a minimal stand-in for the `user` table that
    `knowledge_source.uploaded_by` references. P0-3 turned it into the real
    identity table by adding the four columns below; the original three are
    unchanged, so existing rows and foreign keys were untouched by that
    migration.

    `password_hash` is nullable on purpose. The seeded service account
    (`00000000-...-0000`) has none because it never logs in, and
    `auth.passwords.verify_or_dummy` is written so that authenticating
    against a null hash costs exactly what a wrong password costs -- an
    endpoint that rejects a passwordless account faster than a real one has
    told the caller which accounts exist.
    """

    __tablename__ = "app_user"

    # Both defaults on purpose. `server_default` is what a plain SQL INSERT
    # (a migration, a psql session) gets; `default` is what the ORM uses, and
    # it keeps this insert portable to backends without gen_random_uuid --
    # which is what lets the account-creation path be tested against SQLite
    # rather than mocked.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_service_account: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    # Argon2id, parameters embedded in the string. See auth/passwords.py.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ordered value, never an is_admin boolean -- see auth/roles.py for why.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="member"
    )
    # Offboarding without deleting the row, so historical attribution on
    # knowledge_source.uploaded_by keeps resolving to a real person.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RefreshToken(Base):
    """One issued refresh token, so that it can be revoked.

    Access tokens are stateless and short-lived; refresh tokens are stateful
    precisely so logout and revocation do something. Without a row here,
    "log out" would only delete the browser's copy, and a stolen token would
    stay valid for its full fourteen days.

    Refresh is *rotating*: presenting a refresh token revokes it and issues a
    new pair. So a token being presented twice means either a race or a
    theft, and this table is what makes the difference observable --
    `revoked_at` is already set on the second presentation. The router treats
    that as compromise and revokes every token for the user, which logs the
    thief and the victim out together and forces a password-backed login.

    Rows are small and expire on their own; `expires_at` is indexed so a
    sweep can delete the dead ones in one statement.
    """

    __tablename__ = "refresh_token"

    # The token's `jti` claim. Storing the id rather than the token means a
    # database leak does not hand over usable credentials.
    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Free-text, truncated, and only ever shown back to the account's owner.
    # Enough to answer "was that me?" on a session list; deliberately not a
    # fingerprint.
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)


class RateLimitBucket(Base):
    """One fixed window of one rate-limit key.

    In the database rather than in a dict, for the reason P2-5 already
    established for ticket idempotency and crawl discovery: a counter held
    in process memory is per-instance, so N instances multiply every limit by
    N and a restart forgets that anyone was ever throttled. A limiter with
    those properties is not a limit.

    The primary key is (key, window_start), which is what makes the increment
    a single atomic `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`. Two
    concurrent requests cannot both read 4 and both write 5.
    """

    __tablename__ = "rate_limit_bucket"

    # "<scope>:<principal>", e.g. "chat:9f1e...", "login-ip:203.0.113.4".
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


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
    # `with_variant(JSON, "sqlite")` costs nothing on Postgres -- JSONB is
    # still what production gets -- and lets this table be created on an
    # in-memory SQLite database. Without it the SQLite compiler cannot
    # render JSONB at all, so any test that needs a knowledge_source_version
    # row has to mock the query instead of running it. Same reasoning, and
    # the same fix, as `crawl_discovery` below.
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
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
    # Postgres gets pgvector's real type -- that is what the HNSW index and
    # the `<=>` distance operator need, and `with_variant` leaves it
    # untouched there. The SQLite variant exists only so the table can be
    # *created* off Postgres: `/admin/stats` counts this table, and without
    # it any test of that endpoint has to mock the query rather than run
    # it. Nothing off Postgres can do similarity search, and nothing tries.
    embedding: Mapped[list[float]] = mapped_column(
        Vector(768).with_variant(Text(), "sqlite"), nullable=False
    )
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


TicketStatusEnum = SAEnum("OPEN", "IN_PROGRESS", "RESOLVED", name="ticket_status")


class Ticket(Base):
    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ticket_idempotency_key"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    # The customer's answer to the clarifying question the Ticket Agent asks
    # before collecting the email. Nullable: escalation paths open a ticket
    # without asking, and every row created before this column existed has none.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Server-derived key ("<thread_id>:<n>") deduplicating a retried create.
    #
    # Idempotency used to live only in a Python dict on one TicketStore
    # instance, which made the guarantee per-process: two API instances, or
    # one instance restarted between the retry and the original, would each
    # see an empty dict and book a second ticket for the same request --
    # and send the customer a second confirmation email. The unique index
    # below moves the guarantee to the database, where it holds across
    # processes, restarts and deploys.
    #
    # Nullable because callers may omit the key (an escalation path that has
    # no thread to derive one from); Postgres does not consider two NULLs
    # equal, so unkeyed tickets are never deduplicated against each other.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(TicketStatusEnum, nullable=False, server_default="OPEN")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Crawl discovery review state
# ---------------------------------------------------------------------------


class CrawlDiscovery(Base):
    """A pending site-crawl discovery, held between `POST /ingest/crawl/discover`
    and the `.../confirm` that acts on it.

    This was a module-level dict in `api/ingest.py`. That made the review step
    silently instance-affine: discover on instance A, confirm on instance B,
    and the confirm returned 404 "unknown or expired discovery_id" even though
    nothing had expired -- an error message that actively misled whoever hit
    it. A process restart between the two calls did the same thing.

    Rows are transient by design. `expires_at` carries the TTL that used to be
    computed from `time.monotonic()`, and expired rows are deleted on the way
    past rather than by a scheduled job -- the volume is a handful of rows and
    a sweeper process would be more machinery than the problem deserves.
    """

    __tablename__ = "crawl_discovery"

    discovery_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The DiscoveryResult and the CrawlConfig it was produced under, both as
    # plain JSON. Storing the config alongside the result matters: `confirm`
    # must crawl with the same settings discovery ran with, and re-deriving
    # them from the request would let a caller widen the crawl at confirm time.
    #
    # `with_variant(JSON, "sqlite")` costs nothing on Postgres -- JSONB is
    # still what production gets -- and lets the tests create this table on
    # an in-memory SQLite database. Without it the SQLite compiler cannot
    # render JSONB at all, and the round-trip would have to be tested against
    # a mock, which would not exercise the serialisation this table exists for.
    result: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    config: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )

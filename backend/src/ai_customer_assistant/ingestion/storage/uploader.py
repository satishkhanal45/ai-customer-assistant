"""Orchestrates validate -> checksum -> dedup -> upload -> persist.

Both public entry points (``handle_manual_upload`` and
``handle_crawled_document``) gather origin-specific metadata and then
delegate to the single shared ``create_version_and_upload`` — the only
place that touches both ``client.py`` (MinIO) and ``repository.py``
(Postgres). Validation, checksumming, and file-type resolution are pure
functions with no knowledge of either I/O edge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable
from uuid import UUID, uuid4

from ..crawler.models import CrawlDocument
from . import keys, repository
from .client import SupportsObjectStorage
from .config import (
    FILE_TYPE_BY_MIME,
    ORIGIN_MANUAL_UPLOAD,
    ORIGIN_WEB_CRAWL,
    StorageConfig,
)
from .exceptions import (
    ChecksumMismatchError,
    StorageError,
    UnsupportedMediaTypeError,
    UploadTooLargeError,
)
from .repository import NewSourceMetadata

try:  # pragma: no cover - typing-only import guarded for older SQLAlchemy stubs
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError:  # pragma: no cover
    AsyncSession = object  # type: ignore[assignment,misc]


# --- pure request/outcome data -----------------------------------------

@dataclass(frozen=True, slots=True)
class UploadCandidate:
    origin: str
    data: bytes
    mime_type: str
    source_metadata: NewSourceMetadata
    job_triggered_by: str
    filename: str | None = None
    existing_source_id: UUID | None = None
    extra_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationOk:
    pass


@dataclass(frozen=True, slots=True)
class ValidationFailed:
    error: StorageError


ValidationResult = ValidationOk | ValidationFailed


@dataclass(frozen=True, slots=True)
class Created:
    source_id: UUID
    version_id: UUID
    job_id: UUID
    storage_uri: str


@dataclass(frozen=True, slots=True)
class DuplicateSkipped:
    source_id: UUID
    version_id: UUID


@dataclass(frozen=True, slots=True)
class Rejected:
    error: StorageError


@dataclass(frozen=True, slots=True)
class Failed:
    source_id: UUID
    version_id: UUID
    error: StorageError


UploadOutcome = Created | DuplicateSkipped | Rejected | Failed


# --- pure helpers --------------------------------------------------------

def compute_checksum(data: bytes) -> str:
    """SHA-256 hex digest of the raw bytes. Total, deterministic, no I/O."""
    return hashlib.sha256(data).hexdigest()


def file_type_for(mime_type: str) -> str | None:
    """Resolve a MIME type to a ``knowledge_source_version.file_type`` enum
    value. Returns ``None`` for MIME types with no representable file_type
    (e.g. this is where an XLSX MIME type would land, until the enum is
    migrated — see the Phase 1 plan's schema-gap decision)."""
    return FILE_TYPE_BY_MIME.get(mime_type)


def _check_mime_type(candidate: UploadCandidate, config: StorageConfig) -> ValidationResult:
    allowed = config.allowed_mime_types.get(candidate.origin, frozenset())
    return (
        ValidationOk()
        if candidate.mime_type in allowed
        else ValidationFailed(UnsupportedMediaTypeError(candidate.mime_type, candidate.origin))
    )


def _check_size(candidate: UploadCandidate, config: StorageConfig) -> ValidationResult:
    size = len(candidate.data)
    return (
        ValidationOk()
        if size <= config.max_file_size_bytes
        else ValidationFailed(UploadTooLargeError(size, config.max_file_size_bytes))
    )


# Ordered, extensible validation pipeline. Adding a new rule means
# appending a function here, not adding another branch to an if/elif chain.
_VALIDATORS: tuple[Callable[[UploadCandidate, StorageConfig], ValidationResult], ...] = (
    _check_mime_type,
    _check_size,
)


def validate_candidate(candidate: UploadCandidate, config: StorageConfig) -> ValidationResult:
    """Run every validator in order, short-circuiting on the first
    failure. Pure: no exceptions raised, no I/O, always returns a
    ``ValidationResult``."""
    failures = (
        result
        for validator in _VALIDATORS
        if isinstance(result := validator(candidate, config), ValidationFailed)
    )
    return next(failures, ValidationOk())


def _job_type_for(version_number: int) -> str:
    return "INITIAL_INGEST" if version_number == 1 else "UPDATE"


# --- orchestration (the only place both I/O edges meet) -----------------

async def create_version_and_upload(
    candidate: UploadCandidate,
    config: StorageConfig,
    client: SupportsObjectStorage,
    session: "AsyncSession",
) -> UploadOutcome:
    validation = validate_candidate(candidate, config)
    match validation:
        case ValidationFailed(error=error):
            return Rejected(error)
        case ValidationOk():
            pass

    checksum = compute_checksum(candidate.data)

    existing = await repository.find_version_by_checksum(session, checksum)
    if existing is not None:
        return DuplicateSkipped(source_id=existing.source_id, version_id=existing.version_id)

    source_id = await repository.get_or_create_source(
        session, candidate.existing_source_id, candidate.source_metadata
    )
    version_id = uuid4()
    key = keys.build_key(candidate.origin, source_id, version_id, candidate.filename)
    version_number = await repository.next_version_number(session, source_id)

    new_version = await repository.insert_pending_version(
        session,
        version_id=version_id,
        source_id=source_id,
        version_number=version_number,
        checksum=checksum,
        file_type=file_type_for(candidate.mime_type),
        mime_type=candidate.mime_type,
        file_size_bytes=len(candidate.data),
        storage_uri=key,
        metadata=candidate.extra_metadata,
    )

    try:
        client.put_object(key, candidate.data, candidate.mime_type)
        _verify_integrity(client, key, expected_size=len(candidate.data))
    except StorageError as error:
        cleanup_error = _safe_delete(client, key)
        detail = (
            str(error)
            if cleanup_error is None
            else f"{error} (cleanup of partial object also failed: {cleanup_error})"
        )
        await repository.mark_version_failed(session, new_version.version_id, detail)
        await session.commit()
        return Failed(source_id=source_id, version_id=new_version.version_id, error=error)

    await repository.mark_version_ingested(session, new_version.version_id)
    job_id = await repository.enqueue_ingestion_job(
        session,
        source_id=source_id,
        version_id=new_version.version_id,
        job_type=_job_type_for(version_number),
        triggered_by=candidate.job_triggered_by,
    )
    await session.commit()
    return Created(
        source_id=source_id, version_id=new_version.version_id, job_id=job_id, storage_uri=key
    )


def _verify_integrity(client: SupportsObjectStorage, key: str, expected_size: int) -> None:
    """Post-upload corruption check (Phase 1 open question #4): confirm the
    object MinIO now reports matches what we sent, by size. Raises
    ``ChecksumMismatchError`` on mismatch so the caller's except-block
    handles it identically to any other upload failure."""
    stat = client.stat_object(key)
    if stat.size_bytes != expected_size:
        raise ChecksumMismatchError(key, expected=str(expected_size), actual=str(stat.size_bytes))


def _safe_delete(client: SupportsObjectStorage, key: str) -> StorageError | None:
    """Best-effort cleanup of a partially/corruptly uploaded object. Never
    lets a cleanup failure mask the original error — returns it instead of
    raising."""
    try:
        client.delete_object(key)
        return None
    except StorageError as cleanup_error:
        return cleanup_error


# --- public entry points --------------------------------------------------

async def handle_manual_upload(
    *,
    session: "AsyncSession",
    client: SupportsObjectStorage,
    config: StorageConfig,
    data: bytes,
    filename: str,
    mime_type: str,
    uploaded_by: UUID,
    target_source_id: UUID | None = None,
    source_name: str | None = None,
) -> UploadOutcome:
    """Admin-portal upload entry point. ``target_source_id`` is supplied
    when the admin is explicitly adding a new version to an existing
    document; omitted, a brand-new ``knowledge_source`` is created."""
    candidate = UploadCandidate(
        origin=ORIGIN_MANUAL_UPLOAD,
        data=data,
        mime_type=mime_type,
        filename=filename,
        existing_source_id=target_source_id,
        source_metadata=NewSourceMetadata(
            source_name=source_name or filename,
            source_type="FILE_UPLOAD",
            origin_system="manual_upload",
            external_reference_id=None,
            uploaded_by=uploaded_by,
        ),
        job_triggered_by=f"admin:{uploaded_by}",
    )
    return await create_version_and_upload(candidate, config, client, session)


async def handle_crawled_document(
    *,
    session: "AsyncSession",
    client: SupportsObjectStorage,
    config: StorageConfig,
    doc: CrawlDocument,
    triggered_by: str = "system:scheduler",
) -> UploadOutcome:
    """Crawler-output entry point. Called once per successfully crawled
    page (``doc.error is None``) by ``worker.py``."""
    existing_source_id = await repository.find_source_by_external_reference(
        session, origin_system="web_crawl", external_reference_id=doc.url
    )
    candidate = UploadCandidate(
        origin=ORIGIN_WEB_CRAWL,
        data=doc.markdown.encode("utf-8"),
        mime_type="text/markdown",
        existing_source_id=existing_source_id,
        source_metadata=NewSourceMetadata(
            source_name=doc.url,
            source_type="EXTERNAL_INTEGRATION",
            origin_system="web_crawl",
            external_reference_id=doc.url,
            uploaded_by=None,
        ),
        job_triggered_by=triggered_by,
    )
    return await create_version_and_upload(candidate, config, client, session)

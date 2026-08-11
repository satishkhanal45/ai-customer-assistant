"""
Pure logic for ingestion_flow.md step 1: "A file lands — checksum checked
against every version, of every document."

Nothing here touches the database. `classify_upload` is a total, pure
function: given a checksum and what a repository lookup found, it decides
which of the three outcomes applies. The I/O (looking the checksum up,
inserting rows) lives in the queue/repository layer, which calls this.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID


def compute_checksum(raw_bytes: bytes) -> str:
    """SHA-256 hex digest of the raw file, per knowledge_source_version.checksum."""
    return hashlib.sha256(raw_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class NewDocument:
    """No existing source for this content; insert source + version #1."""


@dataclass(frozen=True, slots=True)
class NewVersion:
    """Existing logical document, genuinely new content; insert version N+1."""

    source_id: UUID
    next_version_number: int


@dataclass(frozen=True, slots=True)
class DuplicateContent:
    """Checksum already exists somewhere; skip ingestion entirely."""

    existing_version_id: UUID


UploadClassification = NewDocument | NewVersion | DuplicateContent


def classify_upload(
    *,
    checksum_match: UUID | None,
    existing_source_id: UUID | None,
    latest_version_number: int | None,
) -> UploadClassification:
    """
    Decide which of the three step-1 branches applies.

    Args:
        checksum_match: version_id of any existing row whose checksum equals
            this upload's checksum (global lookup), or None.
        existing_source_id: source_id of the logical document this upload
            belongs to, if the caller already knows it (e.g. re-upload to
            the same source), or None for a brand-new document.
        latest_version_number: the highest version_number already recorded
            under existing_source_id, or None if there is no such source.

    Returns:
        DuplicateContent  -- checksum found anywhere -> skip ingestion.
        NewVersion        -- known source, new content -> version N+1.
        NewDocument        -- unknown source, new content -> version 1.
    """
    dispatch = {
        True: lambda: DuplicateContent(existing_version_id=checksum_match),
        False: lambda: (
            NewVersion(
                source_id=existing_source_id,
                next_version_number=latest_version_number + 1,
            )
            if existing_source_id is not None
            else NewDocument()
        ),
    }
    return dispatch[checksum_match is not None]()

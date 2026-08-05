from uuid import uuid4

from ingestion.dedup import (
    DuplicateContent,
    NewDocument,
    NewVersion,
    classify_upload,
    compute_checksum,
)


def test_compute_checksum_is_deterministic():
    assert compute_checksum(b"hello") == compute_checksum(b"hello")
    assert compute_checksum(b"hello") != compute_checksum(b"world")


def test_classify_upload_duplicate_wins_regardless_of_source():
    existing_version_id = uuid4()
    result = classify_upload(
        checksum_match=existing_version_id,
        existing_source_id=uuid4(),
        latest_version_number=3,
    )
    assert result == DuplicateContent(existing_version_id=existing_version_id)


def test_classify_upload_new_document_when_no_source():
    result = classify_upload(
        checksum_match=None, existing_source_id=None, latest_version_number=None
    )
    assert result == NewDocument()


def test_classify_upload_new_version_increments():
    source_id = uuid4()
    result = classify_upload(
        checksum_match=None, existing_source_id=source_id, latest_version_number=4
    )
    assert result == NewVersion(source_id=source_id, next_version_number=5)

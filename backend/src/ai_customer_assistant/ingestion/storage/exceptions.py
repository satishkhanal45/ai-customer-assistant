"""Exception types for the storage layer.

These are plain data carriers (no behavior beyond ``Exception``'s own)
raised by ``client.py`` when the MinIO SDK reports a problem, and by
``uploader.py`` when a candidate upload fails validation before any I/O
happens. Nothing downstream needs to know these wrap the MinIO SDK
specifically — the exception names describe outcomes, not implementation.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every error raised by the storage layer."""


class BucketUnavailableError(StorageError):
    """The configured bucket could not be reached (connectivity, auth,
    or the bucket itself does not exist)."""


class ObjectNotFoundError(StorageError):
    """No object exists at the requested key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"No object found at key: {key}")
        self.key = key


class ChecksumMismatchError(StorageError):
    """The object read back from storage does not match the checksum
    computed before upload — indicates corruption in transit."""

    def __init__(self, key: str, expected: str, actual: str) -> None:
        super().__init__(
            f"Checksum mismatch for {key}: expected {expected}, got {actual}"
        )
        self.key = key
        self.expected = expected
        self.actual = actual


class UploadTooLargeError(StorageError):
    """The candidate's byte size exceeds ``StorageConfig.max_file_size_bytes``."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        super().__init__(f"{size_bytes} bytes exceeds the {max_bytes}-byte limit")
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class UnsupportedMediaTypeError(StorageError):
    """The candidate's MIME type is not in the allow-list for its origin."""

    def __init__(self, mime_type: str, origin: str) -> None:
        super().__init__(f"MIME type {mime_type!r} is not permitted for origin {origin!r}")
        self.mime_type = mime_type
        self.origin = origin

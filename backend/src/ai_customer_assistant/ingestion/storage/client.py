"""The single I/O boundary onto MinIO's S3 API.

Every other module in ``storage/`` treats ``StorageClient`` as an opaque
capability — nothing outside this file imports the ``minio`` SDK, mirroring
how ``fetcher.py`` is the crawler package's only network boundary. SDK-level
exceptions are translated here into ``storage.exceptions`` types so the
rest of the codebase never has to know MinIO's error taxonomy.

Structured for a future multipart upload path: ``put_object`` accepts a
plain ``bytes`` payload today, but the public surface (a single method
taking a key and data, returning an ``ObjectInfo``) would not need to
change if the implementation later streamed large payloads in parts —
only the body of ``put_object`` would.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from minio import Minio
from minio.error import S3Error

from .config import StorageConfig
from .exceptions import (
    BucketUnavailableError,
    ObjectNotFoundError,
    StorageError,
)


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Result of a successful ``put_object`` call."""

    key: str
    etag: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ObjectStat:
    """Result of a ``stat_object`` call — metadata without the bytes."""

    key: str
    etag: str
    size_bytes: int
    content_type: str | None


class SupportsObjectStorage(Protocol):
    """Structural type the rest of ``storage/`` (and tests) depend on,
    rather than depending on ``StorageClient`` — or the ``minio`` package —
    directly. A fake implementing this protocol is sufficient for tests."""

    def put_object(self, key: str, data: bytes, content_type: str) -> ObjectInfo: ...
    def get_object(self, key: str) -> bytes: ...
    def delete_object(self, key: str) -> None: ...
    def object_exists(self, key: str) -> bool: ...
    def stat_object(self, key: str) -> ObjectStat: ...
    def presigned_get_url(self, key: str, expiry: timedelta) -> str: ...


# S3Error codes we care about distinguishing, mapped to the exception each
# should become. A dispatch table instead of an if/elif chain on
# ``error.code``; anything not listed falls through to the generic
# ``StorageError`` at the call site.
_S3_ERROR_MAP: dict[str, type[StorageError]] = {
    "NoSuchBucket": BucketUnavailableError,
    "NoSuchKey": ObjectNotFoundError,
}


def _translate(key: str, error: S3Error) -> StorageError:
    exc_type = _S3_ERROR_MAP.get(error.code, StorageError)
    return (
        exc_type(key)
        if exc_type is ObjectNotFoundError
        else exc_type(str(error))
    )


class StorageClient:
    """Thin wrapper around the MinIO SDK. Construct once per process from
    a ``StorageConfig`` and share the instance."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._bucket = config.bucket_name
        self._minio = Minio(
            endpoint=config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            secure=config.secure,
            region=config.region,
        )

    def ensure_bucket(self) -> None:
        """Fail-fast connectivity/bucket-existence check, intended to run
        once at application startup."""
        try:
            if not self._minio.bucket_exists(self._bucket):
                raise BucketUnavailableError(
                    f"Bucket {self._bucket!r} does not exist at {self._config.endpoint}"
                )
        except S3Error as error:
            raise _translate(self._bucket, error) from error

    def put_object(self, key: str, data: bytes, content_type: str) -> ObjectInfo:
        try:
            result = self._minio.put_object(
                bucket_name=self._bucket,
                object_name=key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except S3Error as error:
            raise _translate(key, error) from error
        return ObjectInfo(key=key, etag=result.etag, size_bytes=len(data))

    def get_object(self, key: str) -> bytes:
        response = None
        try:
            response = self._minio.get_object(self._bucket, key)
            return response.read()
        except S3Error as error:
            raise _translate(key, error) from error
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def delete_object(self, key: str) -> None:
        try:
            self._minio.remove_object(self._bucket, key)
        except S3Error as error:
            raise _translate(key, error) from error

    def object_exists(self, key: str) -> bool:
        try:
            self._minio.stat_object(self._bucket, key)
            return True
        except S3Error as error:
            if error.code == "NoSuchKey":
                return False
            raise _translate(key, error) from error

    def stat_object(self, key: str) -> ObjectStat:
        try:
            stat = self._minio.stat_object(self._bucket, key)
        except S3Error as error:
            raise _translate(key, error) from error
        return ObjectStat(
            key=key,
            etag=stat.etag,
            size_bytes=stat.size,
            content_type=stat.content_type,
        )

    def presigned_get_url(self, key: str, expiry: timedelta) -> str:
        try:
            return self._minio.presigned_get_object(
                self._bucket, key, expires=expiry
            )
        except S3Error as error:
            raise _translate(key, error) from error
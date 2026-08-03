"""Configuration for the MinIO-backed storage layer.

Mirrors the style of ``CrawlConfig`` in ``ingestion/crawler``: a frozen
dataclass holding validated, typed connection/behavior settings, populated
once at startup via ``load_storage_config`` and threaded through the rest
of the module as an explicit argument — never read from the environment
again outside this file.

The MIME allow-list is expressed as data here (not as an env var) because
it encodes a schema-level decision — see the Phase 1 plan, "Schema Gap
Decision" — rather than a per-deployment tunable. XLSX/XLS are
intentionally absent from ``manual_upload`` until the ``file_type`` enum
migration lands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Mapping

# --- data layer -------------------------------------------------------

# Origin identifiers used throughout storage/ and repository/. Kept as
# plain string constants (not an enum) so they line up 1:1 with
# knowledge_source.origin_system values ("manual_upload", "web_crawl", ...).
ORIGIN_MANUAL_UPLOAD = "manual_upload"
ORIGIN_WEB_CRAWL = "web_crawl"

ALLOWED_MIME_TYPES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        ORIGIN_MANUAL_UPLOAD: frozenset(
            {
                "application/pdf",
                "text/markdown",
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",  # DOCX
            }
        ),
        ORIGIN_WEB_CRAWL: frozenset({"text/markdown"}),
    }
)

# knowledge_source_version.file_type enum values, keyed by MIME type, for
# the origins/mime-types we actually accept. Pure lookup data — no
# behavior. Anything not present here has no representable file_type and
# must be rejected before this table is ever consulted.
FILE_TYPE_BY_MIME: Mapping[str, str] = MappingProxyType(
    {
        "application/pdf": "PDF",
        "text/markdown": "MD",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    }
)


# --- config object ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StorageConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket_name: str
    secure: bool = True
    region: str | None = None
    presigned_url_expiry: timedelta = field(default_factory=lambda: timedelta(hours=1))
    max_file_size_bytes: int = 50 * 1024 * 1024  # 50 MB
    allowed_mime_types: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: ALLOWED_MIME_TYPES
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def load_storage_config(env: Mapping[str, str] | None = None) -> StorageConfig:
    """Build a ``StorageConfig`` from environment variables.

    Accepts an optional ``env`` mapping (defaults to ``os.environ``) so
    tests can supply a fake environment without mutating process state.
    """
    source = env if env is not None else os.environ

    def get(name: str, default: str | None = None) -> str | None:
        return source.get(name, default)

    def require(name: str) -> str:
        value = get(name)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    return StorageConfig(
        endpoint=require("MINIO_ENDPOINT"),
        access_key=require("MINIO_ACCESS_KEY"),
        secret_key=require("MINIO_SECRET_KEY"),
        bucket_name=get("MINIO_BUCKET_NAME", "knowledge-base") or "knowledge-base",
        secure=(get("MINIO_SECURE", "true") or "true").strip().lower() in {"1", "true", "yes", "on"},
        region=get("MINIO_REGION") or None,
        presigned_url_expiry=timedelta(
            seconds=int(get("MINIO_PRESIGNED_URL_EXPIRY_SECONDS", "3600") or "3600")
        ),
        max_file_size_bytes=int(
            get("MINIO_MAX_FILE_SIZE_BYTES", str(50 * 1024 * 1024)) or str(50 * 1024 * 1024)
        ),
        allowed_mime_types=ALLOWED_MIME_TYPES,
    )
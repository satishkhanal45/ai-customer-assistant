
from __future__ import annotations

import re
from functools import reduce
from uuid import UUID

UPLOADS_PREFIX = "uploads"
CRAWLED_PREFIX = "crawled"
CRAWLED_LEAF = "page.md"

_MAX_SLUG_LENGTH = 128
_DISALLOWED_CHARS = re.compile(r"[^a-z0-9._-]+")
_WHITESPACE = re.compile(r"\s+")
_COLLAPSE_DASHES = re.compile(r"-{2,}")

# Ordered pipeline of pure string transforms, applied left to right via
# reduce — adding a new normalization step means appending to this tuple,
# not editing branching logic.
_SLUG_STEPS: tuple[callable, ...] = (
    str.lower,
    lambda s: _WHITESPACE.sub("-", s.strip()),
    lambda s: _DISALLOWED_CHARS.sub("", s),
    lambda s: _COLLAPSE_DASHES.sub("-", s),
    lambda s: s.strip("-._") or "file",
    lambda s: s[:_MAX_SLUG_LENGTH],
)


def slugify(name: str) -> str:
    """Normalize an arbitrary filename into a bucket-safe, human-legible
    slug. Total: always returns a non-empty string."""
    return reduce(lambda acc, step: step(acc), _SLUG_STEPS, name)


def source_prefix(origin_prefix: str, source_id: UUID) -> str:
    """Prefix that lists every version of one logical document."""
    return f"{origin_prefix}/{source_id}/"


def version_prefix(origin_prefix: str, source_id: UUID, version_id: UUID) -> str:
    """Prefix that lists every object belonging to one specific version."""
    return f"{origin_prefix}/{source_id}/{version_id}/"


def build_upload_key(source_id: UUID, version_id: UUID, filename: str) -> str:
    """Key for a manually uploaded file."""
    return f"{version_prefix(UPLOADS_PREFIX, source_id, version_id)}{slugify(filename)}"


def build_crawled_key(source_id: UUID, version_id: UUID) -> str:
    """Key for one crawled page's markdown."""
    return f"{version_prefix(CRAWLED_PREFIX, source_id, version_id)}{CRAWLED_LEAF}"


# Dispatch table from origin -> key-builder, so callers never branch on
# origin with if/elif; they look the builder up and call it.
KEY_BUILDERS: dict[str, callable] = {
    "manual_upload": build_upload_key,
    "web_crawl": lambda source_id, version_id, _filename=None: build_crawled_key(
        source_id, version_id
    ),
}


def build_key(origin: str, source_id: UUID, version_id: UUID, filename: str | None = None) -> str:
    """Origin-dispatching key builder — the single entry point ``uploader.py``
    actually calls."""
    builder = KEY_BUILDERS[origin]
    return builder(source_id, version_id, filename)
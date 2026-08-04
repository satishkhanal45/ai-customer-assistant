"""
Configuration for the chunking / embedding pipeline.

This module holds ONLY settings — no business logic. It is the single
source of truth for values that chunking.py and embedding.py depend on
(chunk size, overlap, model name, embedding dimension), so those modules
never hardcode a number inline; they always receive a settings instance.

Built on pydantic-settings.BaseSettings to match the rest of this
codebase's existing configuration pattern, and to get environment
variable / .env loading without writing that plumbing by hand.

Settings are frozen after construction: once loaded, a value cannot be
reassigned, which prevents one part of the pipeline from silently
mutating a setting another part already read.
"""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionSettings(BaseSettings):
    """
    Settings for chunking and embedding.

    Confirmed values (see project decisions):
        chunk_size_tokens: 500 tokens per chunk.
        chunk_overlap_tokens: ~75 tokens (15% of chunk_size_tokens).
        embedding_model_name: BAAI/bge-base-en-v1.5.
        embedding_dimension: 768.
        normalize_embeddings: True (required for correct cosine similarity
            at retrieval time; enforced here even though retrieval itself
            is outside this module's responsibility).

    Values are loaded from environment variables prefixed with
    ``INGESTION_`` (e.g. ``INGESTION_CHUNK_SIZE_TOKENS``), falling back to
    the defaults below when unset, and may also be supplied via a
    ``.env`` file per this project's existing convention.
    """

    model_config = SettingsConfigDict(
        env_prefix="INGESTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
        extra="ignore",
    )

    chunk_size_tokens: int = 500
    chunk_overlap_tokens: int = 75

    embedding_model_name: str = "BAAI/bge-base-en-v1.5"
    embedding_dimension: int = 768
    normalize_embeddings: bool = True

    @model_validator(mode="after")
    def _validate_overlap_within_chunk_size(self) -> "IngestionSettings":
        """
        Reject settings where overlap is not strictly smaller than chunk
        size, since an overlap >= chunk size makes the recursive splitter's
        sliding window unable to make forward progress.

        This is a pure validation check — it raises rather than silently
        clamping or otherwise mutating the supplied values.
        """
        is_valid = 0 <= self.chunk_overlap_tokens < self.chunk_size_tokens
        if not is_valid:
            raise ValueError(
                "chunk_overlap_tokens must be >= 0 and strictly less than "
                f"chunk_size_tokens (got chunk_size_tokens="
                f"{self.chunk_size_tokens}, chunk_overlap_tokens="
                f"{self.chunk_overlap_tokens})"
            )
        return self

    @property
    def overlap_ratio(self) -> float:
        """
        Overlap expressed as a fraction of chunk size (e.g. 0.15 for the
        confirmed 500/75 configuration).

        Derived on read rather than stored, so this can never drift out
        of sync with chunk_size_tokens / chunk_overlap_tokens.
        """
        return self.chunk_overlap_tokens / self.chunk_size_tokens


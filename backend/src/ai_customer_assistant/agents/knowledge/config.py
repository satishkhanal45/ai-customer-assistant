"""
config.py — KnowledgeAgentConfig.

Frozen, environment-driven settings for the Knowledge Agent, following the
exact pattern used by `chunk_embed/config.py`'s `IngestionSettings`:
pydantic-settings `BaseSettings`, frozen, with a module-specific env
prefix. Every other module in this package receives a `KnowledgeAgentConfig`
instance via explicit dependency injection — nothing reads environment
variables or defaults directly; only this module does.

Env prefix: `KNOWLEDGE_AGENT_` (e.g. `KNOWLEDGE_AGENT_TOP_K=10`).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import (
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL_NAME,
    DEFAULT_EXTRACTION_CONFIDENCE_THRESHOLD,
    DEFAULT_GROUNDEDNESS_THRESHOLD,
    DEFAULT_MAX_CONTEXT_CHUNKS,
    DEFAULT_MAX_STRUCTURED_FACTS,
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_TOP_K,
)

_DEFAULT_PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"


class KnowledgeAgentConfig(BaseSettings):
    """Immutable configuration injected into every retrieval-stage
    function that needs a tunable value (top_k, thresholds, model names,
    prompt location). No module other than this one reads `os.environ`
    or hardcodes these values."""

    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGE_AGENT_",
        frozen=True,
        extra="forbid",
    )

    # -- Vector search -----------------------------------------------------
    top_k: int = DEFAULT_TOP_K
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    embedding_model_name: str = DEFAULT_EMBEDDING_MODEL_NAME
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION

    # -- Extraction ----------------------------------------------------------
    extraction_confidence_threshold: float = DEFAULT_EXTRACTION_CONFIDENCE_THRESHOLD

    # -- Context assembly ----------------------------------------------------
    max_context_chunks: int = DEFAULT_MAX_CONTEXT_CHUNKS
    max_structured_facts: int = DEFAULT_MAX_STRUCTURED_FACTS

    # -- Generation / groundedness ------------------------------------------
    groundedness_threshold: float = DEFAULT_GROUNDEDNESS_THRESHOLD
    llm_provider: str = "anthropic"
    llm_model_name: str = "claude-sonnet-5"
    rewrite_model_name: str = "claude-sonnet-5"

    # -- Prompts ---------------------------------------------------------------
    prompts_dir: Path = _DEFAULT_PROMPTS_DIR

    @field_validator("top_k", "max_context_chunks", "max_structured_facts")
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("embedding_dimension")
    @classmethod
    def _embedding_dimension_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding_dimension must be a positive integer")
        return value

    @field_validator(
        "similarity_threshold",
        "extraction_confidence_threshold",
        "groundedness_threshold",
    )
    @classmethod
    def _must_be_a_probability(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("must be between 0.0 and 1.0 inclusive")
        return value

    @model_validator(mode="after")
    def _top_k_fits_within_context_budget(self) -> "KnowledgeAgentConfig":
        # top_k vector results plus structured facts must be able to fit
        # inside the context budget the prompt builder is allowed to use;
        # catching this at config-construction time surfaces a
        # misconfiguration immediately rather than as a truncated,
        # silently-degraded prompt at request time.
        if self.top_k > self.max_context_chunks:
            raise ValueError(
                "top_k must not exceed max_context_chunks "
                f"(top_k={self.top_k}, max_context_chunks={self.max_context_chunks})"
            )
        return self
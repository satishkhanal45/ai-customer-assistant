"""
constants.py — static constants for the Knowledge Agent.

Pure data layer: literal values and small immutable lookup tables that
other modules reference by name instead of hardcoding magic numbers or
strings inline. Nothing here has behavior. Defaults that are meant to be
overridable at runtime live in `config.py` (`KnowledgeAgentConfig`) and
*reference* these constants as their default values — this file is the
single place those defaults are defined.
"""

from __future__ import annotations

from types import MappingProxyType

# --------------------------------------------------------------------------
# Retrieval strategy literals
# --------------------------------------------------------------------------

STRATEGY_STRUCTURED: str = "structured"
STRATEGY_VECTOR: str = "vector"
STRATEGY_HYBRID: str = "hybrid"

RETRIEVAL_STRATEGIES: tuple[str, ...] = (
    STRATEGY_STRUCTURED,
    STRATEGY_VECTOR,
    STRATEGY_HYBRID,
)

# --------------------------------------------------------------------------
# schema.md `attribute.value_type` CHECK constraint — the only five
# values ontology.py's value_type_for_attribute() is allowed to return.
# --------------------------------------------------------------------------

VALUE_TYPE_STRING: str = "string"
VALUE_TYPE_NUMBER: str = "number"
VALUE_TYPE_BOOLEAN: str = "boolean"
VALUE_TYPE_DATE: str = "date"
VALUE_TYPE_JSON: str = "json"

VALID_VALUE_TYPES: tuple[str, ...] = (
    VALUE_TYPE_STRING,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_BOOLEAN,
    VALUE_TYPE_DATE,
    VALUE_TYPE_JSON,
)

# --------------------------------------------------------------------------
# knowledge_source_version.status — only INDEXED rows are ever eligible
# for vector search, per the mandated join contract.
# --------------------------------------------------------------------------

VERSION_STATUS_INDEXED: str = "INDEXED"

# --------------------------------------------------------------------------
# knowledge_source_entity_map.relationship_type — the value that marks a
# chunk as linked to a resolved business entity.
# --------------------------------------------------------------------------

RELATIONSHIP_TYPE_DERIVED_CHUNK: str = "DERIVED_CHUNK"

# --------------------------------------------------------------------------
# Retrieval defaults (referenced by config.py as field defaults)
# --------------------------------------------------------------------------

DEFAULT_TOP_K: int = 8
DEFAULT_SIMILARITY_THRESHOLD: float = 0.70
DEFAULT_EXTRACTION_CONFIDENCE_THRESHOLD: float = 0.55
DEFAULT_MAX_CONTEXT_CHUNKS: int = 12
DEFAULT_MAX_STRUCTURED_FACTS: int = 20
DEFAULT_GROUNDEDNESS_THRESHOLD: float = 0.60

# --------------------------------------------------------------------------
# Embedding model — must match chunk_embed's IngestionSettings so
# query-time and index-time vectors live in the same space.
# --------------------------------------------------------------------------

DEFAULT_EMBEDDING_MODEL_NAME: str = "BAAI/bge-base-en-v1.5"
DEFAULT_EMBEDDING_DIMENSION: int = 768

# --------------------------------------------------------------------------
# Prompt template file names (relative to KnowledgeAgentConfig.prompts_dir)
# --------------------------------------------------------------------------

PROMPT_TEMPLATE_REWRITE: str = "rewrite.md"
PROMPT_TEMPLATE_EXTRACTION: str = "extraction.md"
PROMPT_TEMPLATE_ANSWER: str = "answer.md"

PROMPT_TEMPLATE_NAMES: tuple[str, ...] = (
    PROMPT_TEMPLATE_REWRITE,
    PROMPT_TEMPLATE_EXTRACTION,
    PROMPT_TEMPLATE_ANSWER,
)

# --------------------------------------------------------------------------
# Context builder section headers — kept here (not hardcoded inline in
# context_builder.py) so wording changes don't touch logic.
# --------------------------------------------------------------------------

CONTEXT_SECTION_STRUCTURED_FACTS: str = "Structured Facts"
CONTEXT_SECTION_RELEVANT_DOCUMENTATION: str = "Relevant Documentation"

# --------------------------------------------------------------------------
# Ranking weights — how much each signal contributes to a RankedResult's
# ordering. Kept as a mapping so ranking.py can fold over it declaratively
# rather than branching per signal.
# --------------------------------------------------------------------------

RANKING_WEIGHTS: MappingProxyType[str, float] = MappingProxyType(
    {
        "exact_structured_match": 1.00,
        "semantic_similarity": 0.70,
        "extraction_confidence": 0.20,
        "source_freshness": 0.10,
    }
)
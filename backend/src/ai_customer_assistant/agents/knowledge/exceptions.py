"""
exceptions.py — typed exception hierarchy for the Knowledge Agent.

Data-only module: every exception carries the structured context a caller
needs to react programmatically (entity_type, attribute, query text, etc.)
rather than relying on parsing a message string. No behavior, no logic —
just a total, explicit vocabulary of everything that can go wrong across
the retrieval pipeline, so every other module can `raise` instead of
returning `None` on a failure path.

Deliberately NOT frozen, unlike every other dataclass in this package:
exceptions are runtime objects Python itself mutates as they propagate
(setting `__traceback__`, `__cause__`, `__context__`) — this is
core language/framework behavior (confirmed: LangGraph's own internal
context handling does `exc.__traceback__ = traceback` while unwinding a
node's error), not business data flowing through a pure function. A
frozen dataclass blocks *all* attribute assignment, including those
dunder fields, which turns a normal exception propagation into a
second, confusing `FrozenInstanceError` that masks the original error.
Every other type in this package stays frozen per the immutable-first
convention; exceptions are the one deliberate, documented exception to
that rule.

Hierarchy:

    KnowledgeAgentError
    ├── OntologyError
    │   ├── UnknownEntityTypeError
    │   └── UnknownAttributeError
    ├── ExtractionError
    │   └── LowConfidenceExtractionError
    ├── StructuredLookupError
    │   ├── EntityNotFoundError
    │   ├── AmbiguousEntityError
    │   └── AttributeNotFoundError
    ├── VectorSearchError
    │   └── EmptyRetrievalError
    ├── HybridStrategyError
    ├── ContextBuildError
    ├── PromptBuildError
    │   └── PromptTemplateNotFoundError
    └── LLMGenerationError
        └── UngroundedResponseError
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KnowledgeAgentError(Exception):
    """Base type for every error raised anywhere in the Knowledge Agent."""

    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# --------------------------------------------------------------------------
# Ontology
# --------------------------------------------------------------------------


@dataclass
class OntologyError(KnowledgeAgentError):
    """Base type for ontology normalization/lookup failures."""


@dataclass
class UnknownEntityTypeError(OntologyError):
    """Raised when a term cannot be canonicalized to any known entity type."""

    candidate_text: str = ""


@dataclass
class UnknownAttributeError(OntologyError):
    """Raised when a term cannot be canonicalized to an attribute of the
    given entity type (or the entity type itself is unrecognized)."""

    entity_type: str = ""
    candidate_text: str = ""


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class ExtractionError(KnowledgeAgentError):
    """Base type for structured query extraction failures."""


@dataclass
class LowConfidenceExtractionError(ExtractionError):
    """Raised when extraction confidence falls below the configured
    threshold and the caller has chosen not to proceed with a guess."""

    confidence: float = 0.0
    threshold: float = 0.0


# --------------------------------------------------------------------------
# Structured lookup
# --------------------------------------------------------------------------


@dataclass
class StructuredLookupError(KnowledgeAgentError):
    """Base type for deterministic EAV lookup failures."""


@dataclass
class EntityNotFoundError(StructuredLookupError):
    """Raised when no `entity` row matches the requested (entity_type, label)."""

    entity_type: str = ""
    entity_label: str = ""


@dataclass
class AmbiguousEntityError(StructuredLookupError):
    """Raised when more than one `entity` row matches the requested lookup
    and no further filter disambiguates them."""

    entity_type: str = ""
    entity_label: str = ""
    candidate_count: int = 0


@dataclass
class AttributeNotFoundError(StructuredLookupError):
    """Raised when the entity exists but has no `value` row for the
    requested attribute."""

    entity_type: str = ""
    entity_label: str = ""
    attribute: str = ""


# --------------------------------------------------------------------------
# Vector search
# --------------------------------------------------------------------------


@dataclass
class VectorSearchError(KnowledgeAgentError):
    """Base type for pgvector similarity search failures."""


@dataclass
class EmptyRetrievalError(VectorSearchError):
    """Raised when a vector search returns zero chunks above threshold,
    letting the caller decide whether that's fatal or a fallback signal."""

    query_text: str = ""
    top_k: int = 0


# --------------------------------------------------------------------------
# Hybrid strategy
# --------------------------------------------------------------------------


@dataclass
class HybridStrategyError(KnowledgeAgentError):
    """Raised when `decide_strategy` cannot resolve a retrieval strategy
    from the given StructuredQuery (should be unreachable if `hybrid.py`'s
    dispatch table is total, but kept explicit rather than silently
    defaulting)."""


# --------------------------------------------------------------------------
# Context / prompt construction
# --------------------------------------------------------------------------


@dataclass
class ContextBuildError(KnowledgeAgentError):
    """Raised when structured facts and retrieved chunks cannot be
    assembled into a valid BuiltContext."""


@dataclass
class PromptBuildError(KnowledgeAgentError):
    """Base type for prompt construction failures."""


@dataclass
class PromptTemplateNotFoundError(PromptBuildError):
    """Raised when a required template file is missing from `prompts/`."""

    template_name: str = ""
    prompts_dir: str = ""


# --------------------------------------------------------------------------
# LLM generation
# --------------------------------------------------------------------------


@dataclass
class LLMGenerationError(KnowledgeAgentError):
    """Raised when the underlying LLM call fails. The original exception
    should be chained via `raise LLMGenerationError(...) from original`,
    never swallowed."""


@dataclass
class UngroundedResponseError(LLMGenerationError):
    """Raised when the groundedness check determines the generated answer
    is not adequately supported by retrieved context."""

    answer_text: str = ""
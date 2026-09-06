"""
ontology.py — the ingestion extractor's view of the shared ontology.

The vocabulary itself lives in the project-level `ontology` package. This
module used to carry its own ~750-line copy of those tables, deliberately
forked from the Knowledge Agent's copy so that ingestion would not depend on
the agents package. The isolation goal was sound; the duplication was not —
the two copies drifted, and the failure mode was silent data loss rather than
an error (see `ontology/vocabulary.py` for the specific facts affected).

The shared package belongs to neither `agents` nor `ingestion`, so importing
it preserves the original isolation: ingestion still does not depend on the
agents package.

What stays here is this pipeline's *policy* over the vocabulary, which
genuinely differs from the Knowledge Agent's:

  * entity types and attributes are resolved **exact-match + synonym only,
    never fuzzy** — this pipeline writes to the database, and a wrong
    canonical type silently mis-merges two different real-world entities
    forever. difflib is false-positive prone on short strings ("customer" ->
    "Cluster", "plan tier" -> "partner"), so an unrecognized term keeps its
    raw form instead of being guessed at. Retrieval, which only *reads*,
    deliberately does fuzzy-match;
  * `canonicalize_*` raise on a miss (the strict contract `tools.py` relies on);
  * `safe_canonicalize_*` never raise and fall back to the original text,
    because one odd string from the model must not abort a whole document;
  * `formatted_ontology_reference()` renders the prompt block sent on every
    extraction call.

The exception types are also local: they are plain `ValueError` subclasses
with positional arguments, distinct from the Knowledge Agent's keyword-based
hierarchy. Callers of this module keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ontology import matching
from ontology.vocabulary import (  # re-exported: the tables live in the shared package now
    ALL_ENTITY_TYPES,
    ATTRIBUTE_SYNONYMS,
    ATTRIBUTE_VALUE_TYPES,
    DOMAIN_ENTITY_TYPES,
    ENTITY_TYPE_ATTRIBUTES,
    ENTITY_TYPE_SYNONYMS,
    ENTITY_TYPE_TO_DOMAIN,
    RELATION_TYPE_SYNONYMS,
    RELATION_TYPE_VOCABULARY,
    ValueType,
)

__all__ = [
    "ALL_ENTITY_TYPES",
    "ATTRIBUTE_SYNONYMS",
    "ATTRIBUTE_VALUE_TYPES",
    "CanonicalizationResult",
    "DOMAIN_ENTITY_TYPES",
    "ENTITY_TYPE_ATTRIBUTES",
    "ENTITY_TYPE_SYNONYMS",
    "ENTITY_TYPE_TO_DOMAIN",
    "RELATION_TYPE_SYNONYMS",
    "RELATION_TYPE_VOCABULARY",
    "UnknownAttributeError",
    "UnknownEntityTypeError",
    "ValueType",
    "attributes_for_entity_type",
    "canonicalize_attribute",
    "canonicalize_entity_type",
    "canonicalize_relation_type",
    "formatted_ontology_reference",
    "safe_canonicalize_attribute",
    "safe_canonicalize_entity_type",
    "safe_canonicalize_relation_type",
    "value_type_for_attribute",
]


class UnknownEntityTypeError(ValueError):
    """Raised when a candidate cannot be resolved to a canonical entity type."""


class UnknownAttributeError(ValueError):
    """Raised when a candidate cannot be resolved to a canonical attribute."""


@dataclass(frozen=True)
class CanonicalizationResult:
    """A resolved canonical term plus the confidence of the resolution."""

    canonical_term: str
    confidence: float
    alternate_candidates: tuple[str, ...] = ()


def _as_result(match: matching.Match) -> CanonicalizationResult:
    """Translate the shared engine's neutral `Match` into this module's type."""
    return CanonicalizationResult(
        canonical_term=match.canonical_term,
        confidence=match.confidence,
        alternate_candidates=match.alternate_candidates,
    )


# --------------------------------------------------------------------------
# Strict API — raises on a miss.
# --------------------------------------------------------------------------


def attributes_for_entity_type(entity_type: str) -> tuple[str, ...]:
    attributes = matching.attributes_for_entity_type(entity_type)
    if attributes is None:
        raise UnknownEntityTypeError(
            f"{entity_type!r} is not a canonical entity type", entity_type
        )
    return attributes


def value_type_for_attribute(attribute: str) -> ValueType:
    value_type = matching.value_type_for_attribute(attribute)
    if value_type is None:
        raise UnknownAttributeError(
            f"no value_type mapping for attribute {attribute!r}", attribute
        )
    return value_type


def canonicalize_entity_type(candidate_text: str) -> CanonicalizationResult:
    """Exact-match + synonym only. See the module docstring: a fuzzy guess on
    a write path silently mis-merges entities."""
    if not candidate_text or not candidate_text.strip():
        raise UnknownEntityTypeError("empty entity type", candidate_text)
    match = matching.match_entity_type(candidate_text, fuzzy=False)
    if match is None:
        raise UnknownEntityTypeError(
            f"no canonical entity type found for {candidate_text!r}", candidate_text
        )
    return _as_result(match)


def canonicalize_attribute(entity_type: str, candidate_text: str) -> CanonicalizationResult:
    """Same exact-match + synonym policy as entity types."""
    attributes_for_entity_type(entity_type)  # raises UnknownEntityTypeError first

    match = matching.match_attribute(entity_type, candidate_text, fuzzy=False)
    if match is None:
        raise UnknownAttributeError(
            f"no canonical attribute found for {candidate_text!r} "
            f"on entity type {entity_type!r}",
            candidate_text,
        )
    return _as_result(match)


def canonicalize_relation_type(candidate_text: Optional[str]) -> Optional[CanonicalizationResult]:
    """`relation.relation_type` has no CHECK constraint, so a miss degrades to
    a slugified candidate rather than raising. None only when no relation was
    expressed at all."""
    match = matching.match_relation_type(candidate_text)
    return None if match is None else _as_result(match)


# --------------------------------------------------------------------------
# Non-raising wrappers used by the extractor (tools.py) and the persistence
# backstop (persistence.py): never raise, fall back to the original text so
# a genuinely unknown term keeps flowing through unchanged.
# --------------------------------------------------------------------------


def safe_canonicalize_entity_type(candidate_text: Optional[str]) -> str:
    if not candidate_text or not candidate_text.strip():
        return candidate_text or ""
    try:
        return canonicalize_entity_type(candidate_text).canonical_term
    except UnknownEntityTypeError:
        return candidate_text.strip()


def safe_canonicalize_attribute(entity_type: Optional[str], candidate_text: Optional[str]) -> str:
    if not entity_type or not candidate_text or not candidate_text.strip():
        return candidate_text or ""
    try:
        return canonicalize_attribute(entity_type, candidate_text).canonical_term
    except (UnknownEntityTypeError, UnknownAttributeError):
        return candidate_text.strip()


def safe_canonicalize_relation_type(candidate_text: Optional[str]) -> str:
    if not candidate_text or not candidate_text.strip():
        return candidate_text or ""
    result = canonicalize_relation_type(candidate_text)
    return result.canonical_term if result else candidate_text.strip()


# --------------------------------------------------------------------------
# Prompt reference block
# --------------------------------------------------------------------------


def formatted_ontology_reference() -> str:
    """Compact Domain -> Entity Types block for the extraction system prompt,
    assembled from the shared tables so it always stays in sync.

    Entity types ONLY (no per-type attributes): the reference is sent on every
    model call, so keeping it small bounds the token cost of ingestion.
    Attributes are handled by the schema descriptions and by the extraction
    agent's canonicalization, not by listing them all in-context."""
    lines = [
        f"- {domain}: {', '.join(entity_types)}"
        for domain, entity_types in DOMAIN_ENTITY_TYPES.items()
    ]
    return "\n".join(lines)

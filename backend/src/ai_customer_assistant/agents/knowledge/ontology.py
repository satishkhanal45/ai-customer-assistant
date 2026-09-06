"""
ontology.py — the Knowledge Agent's view of the shared ontology.

The vocabulary itself lives in the project-level `ontology` package, which
belongs to neither `agents` nor `ingestion` so both can share it without
depending on each other. This module used to carry its own ~750-line copy of
those tables; the ingestion extractor carried a second one, and the two drifted
apart silently (see `ontology/vocabulary.py` for the specific facts that were
being lost). The tables are gone from here; only this package's *policy* over
them remains.

That policy is what stays local: a term that cannot be canonicalized raises
`UnknownEntityTypeError` / `UnknownAttributeError` from this package's own
`exceptions` module, because the Knowledge Agent treats an unresolvable term
as a signal — the extraction stage lowers its confidence and the graph falls
back to vector search rather than inventing an entity type.

Public API is unchanged; every caller of this module keeps working:

    attributes_for_entity_type(entity_type) -> tuple[str, ...]
    value_type_for_attribute(attribute) -> ValueType
    canonicalize_entity_type(candidate_text) -> CanonicalizationResult
    canonicalize_attribute(entity_type, candidate_text) -> CanonicalizationResult
    canonicalize_relation_type(candidate_text) -> CanonicalizationResult | None

The vocabulary tables are re-exported for callers and tests that read them
directly, so `ontology.ALL_ENTITY_TYPES` and friends resolve exactly as before.
"""

from __future__ import annotations

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
)

from .exceptions import UnknownAttributeError, UnknownEntityTypeError
from .types import CanonicalizationResult, ValueType

__all__ = [
    "ALL_ENTITY_TYPES",
    "ATTRIBUTE_SYNONYMS",
    "ATTRIBUTE_VALUE_TYPES",
    "DOMAIN_ENTITY_TYPES",
    "ENTITY_TYPE_ATTRIBUTES",
    "ENTITY_TYPE_SYNONYMS",
    "ENTITY_TYPE_TO_DOMAIN",
    "RELATION_TYPE_SYNONYMS",
    "RELATION_TYPE_VOCABULARY",
    "attributes_for_entity_type",
    "canonicalize_attribute",
    "canonicalize_entity_type",
    "canonicalize_relation_type",
    "value_type_for_attribute",
]


def _as_result(match: matching.Match) -> CanonicalizationResult:
    """Translate the shared engine's neutral `Match` into this package's type."""
    return CanonicalizationResult(
        canonical_term=match.canonical_term,
        confidence=match.confidence,
        alternate_candidates=match.alternate_candidates,
    )


def attributes_for_entity_type(entity_type: str) -> tuple[str, ...]:
    """The canonical attribute set for a canonical entity type.

    Raises UnknownEntityTypeError if `entity_type` is not itself a canonical
    entity type (call canonicalize_entity_type() first if the text may be raw
    user wording)."""
    attributes = matching.attributes_for_entity_type(entity_type)
    if attributes is None:
        raise UnknownEntityTypeError(
            message=f"{entity_type!r} is not a canonical entity type",
            candidate_text=entity_type,
        )
    return attributes


def value_type_for_attribute(attribute: str) -> ValueType:
    """The CHECK-constraint-compliant value_type for a canonical attribute name.

    Raises UnknownAttributeError if `attribute` is not recognized."""
    value_type = matching.value_type_for_attribute(attribute)
    if value_type is None:
        raise UnknownAttributeError(
            message=f"no value_type mapping for attribute {attribute!r}",
            candidate_text=attribute,
        )
    return value_type


def canonicalize_entity_type(candidate_text: str) -> CanonicalizationResult:
    """Normalize raw user wording (abbreviation, synonym, plural,
    conversational reference) into a canonical `entity.entity_type`.

    Raises UnknownEntityTypeError if no canonical entity type is close enough
    to be a plausible match — the extractor must never invent a new entity
    type when this happens."""
    match = matching.match_entity_type(candidate_text)
    if match is None:
        raise UnknownEntityTypeError(
            message=f"no canonical entity type found for {candidate_text!r}",
            candidate_text=candidate_text,
        )
    return _as_result(match)


def canonicalize_attribute(entity_type: str, candidate_text: str) -> CanonicalizationResult:
    """Normalize raw user wording into a canonical attribute name valid *for
    the given entity type* — attributes are scoped per entity type, so a term
    must resolve within `entity_type`'s own attribute set.

    Raises UnknownEntityTypeError if `entity_type` itself isn't canonical.
    Raises UnknownAttributeError if no attribute of that entity type is close
    enough to be a plausible match."""
    attributes_for_entity_type(entity_type)  # raises UnknownEntityTypeError first

    match = matching.match_attribute(entity_type, candidate_text)
    if match is None:
        raise UnknownAttributeError(
            message=(
                f"no canonical attribute found for {candidate_text!r} "
                f"on entity type {entity_type!r}"
            ),
            entity_type=entity_type,
            candidate_text=candidate_text,
        )
    return _as_result(match)


def canonicalize_relation_type(candidate_text: Optional[str]) -> Optional[CanonicalizationResult]:
    """Normalize raw user wording into a relation_type.

    Unlike entity types and attributes, `relation.relation_type` carries no
    CHECK constraint — so this never raises for an unrecognized term. A
    vocabulary/synonym match returns high confidence; a fuzzy match returns
    proportional confidence; a complete miss still returns a slugified version
    of the candidate at low confidence, since a free-text relation is valid
    schema-wise. Returns None only when no relation was expressed at all."""
    match = matching.match_relation_type(candidate_text)
    return None if match is None else _as_result(match)

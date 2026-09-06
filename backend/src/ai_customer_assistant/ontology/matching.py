"""
matching.py — the pure canonicalization engine over `vocabulary.py`.

Deliberately framework- and package-neutral: every function here returns a
plain `Match` (or `None` for "no plausible candidate") and raises nothing.
Turning a miss into an exception is the *caller's* policy, and the two
callers genuinely differ:

  * `agents/knowledge/ontology.py` raises `UnknownEntityTypeError` /
    `UnknownAttributeError` from its own `exceptions` module, because the
    Knowledge Agent treats an unresolvable term as a signal to fall back to
    vector search;
  * `ingestion/extraction/ontology.py` exposes both raising and `safe_*`
    non-raising variants, because the extractor must never abort a document
    over one odd string the model emitted.

The other policy that stays with the callers is **whether to fuzzy-match at
all**, exposed here as the `fuzzy` flag:

  * retrieval passes `fuzzy=True` — it is interpreting a human's wording, and
    a near-miss guess that turns out wrong merely returns nothing useful;
  * ingestion passes `fuzzy=False` for entity types and attributes — it is
    *writing to the database*, where a wrong canonical type silently
    mis-merges two different real-world entities forever. difflib is
    false-positive prone on short strings ("customer" -> "Cluster",
    "plan tier" -> "partner"), so ingestion keeps the raw string instead of
    guessing.

Relation types fuzzy-match on both sides: `relation.relation_type` has no
CHECK constraint, so a wrong guess there is recoverable free text rather than
a corrupted identity.

Keeping those policies in the adapters — and only the matching here — is what
lets both packages share one vocabulary without either inheriting the
other's semantics.

Everything is pure: no I/O, no mutation, no module-level state beyond the
immutable tables in `vocabulary.py`.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from .vocabulary import (
    ATTRIBUTE_SYNONYMS,
    ATTRIBUTE_VALUE_TYPES,
    ENTITY_TYPE_ATTRIBUTES,
    ValueType,
    _ENTITY_TYPE_EXACT_INDEX,
    _FUZZY_MATCH_COUNT,
    _FUZZY_MATCH_CUTOFF,
    _RELATION_TYPE_EXACT_INDEX,
    _fuzzy_confidence,
    _normalize,
)

__all__ = [
    "Match",
    "attributes_for_entity_type",
    "match_attribute",
    "match_entity_type",
    "match_relation_type",
    "normalize",
    "value_type_for_attribute",
]


@dataclass(frozen=True)
class Match:
    """A resolved canonical term plus how confident the resolution was.

    Neutral counterpart to each package's own `CanonicalizationResult`; the
    adapters convert this into their own type so their public APIs are
    unchanged.
    """

    canonical_term: str
    confidence: float
    alternate_candidates: tuple[str, ...] = ()


def normalize(text: str) -> str:
    """Lowercase, collapse hyphens/underscores to spaces, collapse repeated
    whitespace. Pure and total."""
    return _normalize(text)


def attributes_for_entity_type(entity_type: str) -> Optional[tuple[str, ...]]:
    """The canonical attribute set for a canonical entity type, or None when
    `entity_type` is not itself canonical."""
    return ENTITY_TYPE_ATTRIBUTES.get(entity_type)


def value_type_for_attribute(attribute: str) -> Optional[ValueType]:
    """The CHECK-constraint-compliant value_type for a canonical attribute
    name, or None when the attribute is unrecognized."""
    return ATTRIBUTE_VALUE_TYPES.get(attribute)


def _match_in_index(
    normalized: str, index: Mapping[str, str], *, fuzzy: bool
) -> Optional[Match]:
    """Exact hit first, then (only when `fuzzy`) a bounded difflib match.
    None when nothing qualifies — callers decide what a miss means."""
    if normalized in index:
        return Match(canonical_term=index[normalized], confidence=1.0)

    if not fuzzy:
        return None

    close_keys = difflib.get_close_matches(
        normalized, index.keys(), n=_FUZZY_MATCH_COUNT, cutoff=_FUZZY_MATCH_CUTOFF
    )
    if not close_keys:
        return None

    resolved = tuple(dict.fromkeys(index[key] for key in close_keys))
    best, *alternates = resolved
    return Match(
        canonical_term=best,
        confidence=_fuzzy_confidence(normalized, close_keys[0]),
        alternate_candidates=tuple(alternates),
    )


def match_entity_type(candidate_text: str, *, fuzzy: bool = True) -> Optional[Match]:
    """Resolve raw wording (abbreviation, synonym, plural, conversational
    reference) to a canonical `entity.entity_type`. None when nothing is a
    plausible match — never invent a new entity type.

    Pass `fuzzy=False` on a write path: see the module docstring for why
    ingestion refuses to guess here."""
    if not candidate_text or not candidate_text.strip():
        return None
    return _match_in_index(
        _normalize(candidate_text), _ENTITY_TYPE_EXACT_INDEX, fuzzy=fuzzy
    )


def match_attribute(
    entity_type: str, candidate_text: str, *, fuzzy: bool = True
) -> Optional[Match]:
    """Resolve raw wording to a canonical attribute valid *for that entity
    type*. Attributes are scoped per entity type, so the candidate index is
    built from that type's own attribute set plus whichever global synonyms
    land inside it.

    None when `entity_type` is not canonical, or when no attribute of that
    type qualifies. Pass `fuzzy=False` on a write path.
    """
    valid_attributes = attributes_for_entity_type(entity_type)
    if valid_attributes is None:
        return None
    if not candidate_text or not candidate_text.strip():
        return None

    scoped_index: Mapping[str, str] = MappingProxyType(
        {
            **{_normalize(attribute): attribute for attribute in valid_attributes},
            **{
                _normalize(synonym): canonical
                for synonym, canonical in ATTRIBUTE_SYNONYMS.items()
                if canonical in valid_attributes
            },
        }
    )
    return _match_in_index(_normalize(candidate_text), scoped_index, fuzzy=fuzzy)


def match_relation_type(candidate_text: Optional[str]) -> Optional[Match]:
    """Resolve raw wording to a relation_type.

    `relation.relation_type` carries no CHECK constraint, so a complete miss
    is not an error: it degrades to a slugified form of the candidate at low
    confidence, since free-text relations are schema-valid. Returns None only
    when no relation was expressed at all.
    """
    if candidate_text is None or not candidate_text.strip():
        return None

    normalized = _normalize(candidate_text)
    matched = _match_in_index(normalized, _RELATION_TYPE_EXACT_INDEX, fuzzy=True)
    if matched is not None:
        return matched
    return Match(canonical_term=normalized.replace(" ", "_"), confidence=0.30)

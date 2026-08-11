"""
ontology.py — the Knowledge Agent's ontology.

Two levels, only one of which is persisted:

  Domain (application-level grouping only — NOT persisted; exists purely
  to help humans and the extractor organize Entity Types, has no
  representation in the database whatsoever)
      -> Entity Type (persisted — maps directly to `entity.entity_type`,
         VARCHAR(64); this is the only level ever written to or read
         from the database)

Attributes are NOT a shared namespace: `attribute.namespace` in
schema.md is scoped per entity type, and `attribute.name` is only
unique *within* that namespace (composite unique constraint on
`(namespace, name)`). So `ENTITY_TYPE_ATTRIBUTES` below is one
dispatch table per entity type, not a single flat attribute set — two
entity types are free to each define their own `status` independently.

Public API (the only functions other modules should call):

    attributes_for_entity_type(entity_type) -> tuple[str, ...]
    value_type_for_attribute(attribute) -> ValueType
    canonicalize_entity_type(candidate_text) -> CanonicalizationResult
    canonicalize_attribute(entity_type, candidate_text) -> CanonicalizationResult
    canonicalize_relation_type(candidate_text) -> CanonicalizationResult | None

Everything else in this module (the raw tables, `_normalize`, the
internal indices) is a private implementation detail. This keeps the
public surface small and stable so a later phase can introduce shared
attribute groups, inheritance, or per-domain defaults without changing
any caller outside this file.
"""

from __future__ import annotations

import difflib
from types import MappingProxyType
from typing import Mapping, Optional

from .exceptions import UnknownAttributeError, UnknownEntityTypeError
from .types import CanonicalizationResult, ValueType

# ==========================================================================
# DOMAIN -> ENTITY TYPE (application-level grouping only, never persisted)
# ==========================================================================

DOMAIN_ENTITY_TYPES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "Organization": (
            "Company", "Department", "Team", "Office", "Employee",
            "Role", "Client", "Partner", "Vendor",
        ),
        "Business": (
            "Service", "Consulting Service", "Support Plan", "Pricing Plan",
            "SLA", "Package", "Proposal", "Product", "Project", "Feature",
            "Module", "Component", "API", "Microservice", "Integration",
        ),
        "Technology": (
            "Technology", "Programming Language", "Framework", "Library",
            "Database", "Cloud Platform", "DevOps Tool", "Operating System",
            "Messaging System", "Vector Database",
        ),
        "Software Engineering": (
            "Architecture Pattern", "Design Pattern", "SDLC Phase",
            "Development Process", "Testing Strategy", "Deployment Strategy",
            "Coding Standard", "Git Workflow", "CI/CD Pipeline",
        ),
        "Knowledge": (
            "Knowledge Source", "Knowledge Category", "Document", "FAQ",
            "Glossary Term", "Knowledge Chunk",
        ),
        "Policies": (
            "Policy", "Guideline", "Standard", "Procedure",
            "Employee Handbook", "Training Material", "Benefit", "Leave Type",
        ),
        "Security": (
            "Security Policy", "Security Practice", "Compliance Standard",
            "Authentication Method", "Authorization Method", "Incident",
            "Risk", "Vulnerability",
        ),
        "Infrastructure": (
            "Server", "Environment", "Container", "Cluster", "Storage",
            "Network", "Monitoring Tool", "Backup Strategy",
        ),
        "Business Intelligence": (
            "Industry", "Business Process", "Workflow", "Customer Type",
            "Business Goal", "KPI", "Metric",
        ),
        "Artificial Intelligence": (
            "AI Solution", "RAG Pipeline", "Embedding Model", "AI Model",
            "LLM", "Prompt Template", "Knowledge Graph", "Agent",
            "Workflow Agent",
        ),
        "Documentation": (
            "Case Study", "Whitepaper", "Report", "Meeting", "Release Note",
            "Changelog",
        ),
    }
)

# Flattened for lookups; reverse map kept for documentation / confidence
# scoring only — never written to or read from `entity.entity_type`.
ALL_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type
    for entity_types in DOMAIN_ENTITY_TYPES.values()
    for entity_type in entity_types
)

ENTITY_TYPE_TO_DOMAIN: Mapping[str, str] = MappingProxyType(
    {
        entity_type: domain
        for domain, entity_types in DOMAIN_ENTITY_TYPES.items()
        for entity_type in entity_types
    }
)

# --------------------------------------------------------------------------
# Entity-type synonyms — informal/abbreviated user wording -> canonical
# entity type. Matched after normalization (see _normalize below).
# --------------------------------------------------------------------------

ENTITY_TYPE_SYNONYMS: Mapping[str, str] = MappingProxyType(
    {
        "db": "Database",
        "backend framework": "Framework",
        "vector db": "Vector Database",
        "ai model": "AI Model",
        "chat model": "LLM",
        "embedding": "Embedding Model",
        "api endpoint": "API",
        "micro-service": "Microservice",
        "microservice": "Microservice",
        "release docs": "Release Note",
        "user manual": "Training Material",
        "handbook": "Employee Handbook",
        "faq": "FAQ",
        "kb article": "Document",
        "knowledge base article": "Document",
        "sla": "SLA",
        "org": "Company",
    }
)

# ==========================================================================
# ENTITY TYPE -> ATTRIBUTES (schema-scoped: attribute.namespace = entity type)
# ==========================================================================

# Baseline attributes shared by every entity type, drawn from the "General"
# and "Temporal" sections of the reference vocabulary.
_BASE_ENTITY_ATTRIBUTES: tuple[str, ...] = (
    "name", "description", "status", "tags", "created_at", "updated_at",
)

# Type-specific extensions. Every value here must consist only of names
# present in ATTRIBUTE_VALUE_TYPES below (verified by the module-load-time
# totality check at the bottom of this file).
_ENTITY_TYPE_ATTRIBUTE_EXTENSIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        # -- Organization ---------------------------------------------------
        "Company": ("industry", "website", "country", "city"),
        "Department": ("owner", "team"),
        "Team": ("manager", "department"),
        "Office": ("country", "city", "address"),
        "Employee": ("role", "department", "email", "phone"),
        "Role": ("department",),
        "Client": ("industry", "contract_type", "website"),
        "Partner": ("industry", "contract_type", "website"),
        "Vendor": ("industry", "contract_type", "website"),
        # -- Business ---------------------------------------------------------
        "Service": ("service_level", "pricing_model", "support_level"),
        "Consulting Service": ("service_level", "pricing_model", "estimated_duration"),
        "Support Plan": ("support_level", "pricing_model", "response_time"),
        "Pricing Plan": ("pricing_model", "cost", "budget"),
        "SLA": ("service_level", "response_time", "uptime"),
        "Package": ("pricing_model", "version"),
        "Proposal": ("target_customer", "estimated_duration", "cost"),
        "Product": ("category", "version", "pricing_model"),
        "Project": ("owner", "start_date", "end_date", "budget"),
        "Feature": ("version", "dependencies"),
        "Module": ("version", "dependencies", "programming_language"),
        "Component": ("version", "dependencies", "technology"),
        "API": ("api_type", "protocol", "authentication_method", "version"),
        "Microservice": ("technology", "programming_language", "protocol", "deployment_model"),
        "Integration": ("provider", "authentication_method", "protocol"),
        # -- Technology -------------------------------------------------------
        "Technology": ("category", "version"),
        "Programming Language": ("version",),
        "Framework": ("programming_language", "version"),
        "Library": ("programming_language", "version"),
        "Database": ("technology", "version", "deployment_model"),
        "Cloud Platform": ("provider", "region"),
        "DevOps Tool": ("category", "version"),
        "Operating System": ("version",),
        "Messaging System": ("protocol", "technology"),
        "Vector Database": ("embedding_dimension", "technology", "deployment_model"),
        # -- Software Engineering -----------------------------------------------
        "Architecture Pattern": ("category",),
        "Design Pattern": ("category",),
        "SDLC Phase": ("category",),
        "Development Process": ("category",),
        "Testing Strategy": ("category",),
        "Deployment Strategy": ("environment", "category"),
        "Coding Standard": ("programming_language", "category"),
        "Git Workflow": ("category",),
        "CI/CD Pipeline": ("environment", "technology"),
        # -- Knowledge -----------------------------------------------------------
        "Knowledge Source": ("source_type", "file_type", "checksum"),
        "Knowledge Category": (),
        "Document": ("document_type", "file_type", "source"),
        "FAQ": ("category",),
        "Glossary Term": ("category",),
        "Knowledge Chunk": ("source", "version_number"),
        # -- Policies ---------------------------------------------------------------
        "Policy": ("category", "effective_date", "review_date"),
        "Guideline": ("category",),
        "Standard": ("category", "compliance"),
        "Procedure": ("category", "department"),
        "Employee Handbook": ("effective_date", "version"),
        "Training Material": ("category", "document_type"),
        "Benefit": ("category",),
        "Leave Type": ("category",),
        # -- Security -----------------------------------------------------------------
        "Security Policy": ("compliance", "effective_date", "review_date"),
        "Security Practice": ("category", "compliance"),
        "Compliance Standard": ("compliance", "review_date"),
        "Authentication Method": ("category", "protocol"),
        "Authorization Method": ("category", "protocol"),
        "Incident": ("severity",),
        "Risk": ("risk_level", "category"),
        "Vulnerability": ("severity",),
        # -- Infrastructure ---------------------------------------------------------
        "Server": ("environment", "region", "operating_system"),
        "Environment": ("region", "category"),
        "Container": ("technology", "environment"),
        "Cluster": ("environment", "region", "scalability"),
        "Storage": ("storage_type", "region"),
        "Network": ("protocol", "region"),
        "Monitoring Tool": ("category", "technology"),
        "Backup Strategy": ("backup_frequency", "storage_type"),
        # -- Business Intelligence ----------------------------------------------------
        "Industry": ("category",),
        "Business Process": ("category", "owner"),
        "Workflow": ("category", "owner"),
        "Customer Type": ("category",),
        "Business Goal": ("category", "owner"),
        "KPI": ("category", "performance"),
        "Metric": ("category", "performance"),
        # -- Artificial Intelligence --------------------------------------------------
        "AI Solution": ("category", "provider", "llm_provider"),
        "RAG Pipeline": ("retrieval_method", "embedding_dimension", "vector_database"),
        "Embedding Model": ("provider", "embedding_dimension"),
        "AI Model": ("provider", "model_name", "version"),
        "LLM": ("provider", "model_name", "version"),
        "Prompt Template": ("category", "version"),
        "Knowledge Graph": ("category", "technology"),
        "Agent": ("category", "llm_provider"),
        "Workflow Agent": ("category", "llm_provider"),
        # -- Documentation --------------------------------------------------------------
        "Case Study": ("category", "industry"),
        "Whitepaper": ("category", "release_date"),
        "Report": ("category", "release_date"),
        "Meeting": ("category", "start_date"),
        "Release Note": ("version", "release_date"),
        "Changelog": ("version", "release_date"),
    }
)

ENTITY_TYPE_ATTRIBUTES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        entity_type: _BASE_ENTITY_ATTRIBUTES + extra
        for entity_type, extra in _ENTITY_TYPE_ATTRIBUTE_EXTENSIONS.items()
    }
)

# --------------------------------------------------------------------------
# Attribute synonyms — informal wording -> canonical attribute name.
# Global (not per entity type); canonicalize_attribute() filters this down
# to whichever synonyms resolve to an attribute valid for the given
# entity type.
# --------------------------------------------------------------------------

ATTRIBUTE_SYNONYMS: Mapping[str, str] = MappingProxyType(
    {
        "phone number": "phone",
        "mobile": "phone",
        "mobile number": "phone",
        "mail": "email",
        "e-mail": "email",
        "created on": "created_at",
        "date created": "created_at",
        "last modified": "updated_at",
        "modified on": "updated_at",
        "owner name": "owner",
        "assigned to": "assigned_to",
        "point of contact": "owner",
    }
)

# ==========================================================================
# ATTRIBUTE -> VALUE_TYPE (must satisfy attribute.value_type CHECK
# constraint: string | number | boolean | date | json)
# ==========================================================================

ATTRIBUTE_VALUE_TYPES: Mapping[str, ValueType] = MappingProxyType(
    {
        # General
        "name": "string", "title": "string", "description": "string",
        "summary": "string", "category": "string", "subtype": "string",
        "status": "string", "priority": "string", "version": "string",
        "tags": "json", "role": "string",
        # Ownership
        "owner": "string", "department": "string", "team": "string",
        "manager": "string", "assigned_to": "string", "maintained_by": "string",
        "created_by": "string", "approved_by": "string",
        # Temporal
        "created_at": "date", "updated_at": "date", "effective_date": "date",
        "review_date": "date", "expiry_date": "date", "start_date": "date",
        "end_date": "date", "release_date": "date",
        # Business
        "industry": "string", "business_model": "string",
        "target_customer": "string", "pricing_model": "string",
        "support_level": "string", "service_level": "string",
        "estimated_duration": "string", "contract_type": "string",
        # Technology
        "technology": "string", "programming_language": "string",
        "framework": "string", "database": "string", "cloud_provider": "string",
        "deployment_model": "string", "architecture": "string",
        "protocol": "string", "api_type": "string",
        "authentication_method": "string",
        # Security
        "encryption": "string", "compliance": "string", "severity": "string",
        "confidentiality": "string", "availability": "string",
        "integrity": "string", "risk_level": "string",
        # Knowledge
        "document_type": "string", "source": "string", "source_type": "string",
        "file_type": "string", "checksum": "string", "version_number": "number",
        # Artificial Intelligence
        "model_name": "string", "provider": "string",
        "embedding_dimension": "number", "vector_database": "string",
        "chunk_size": "number", "retrieval_method": "string",
        "llm_provider": "string",
        # Infrastructure
        "environment": "string", "region": "string",
        "operating_system": "string", "storage_type": "string",
        "backup_frequency": "string", "monitoring_tool": "string",
        # Performance
        "performance": "string", "response_time": "number", "uptime": "number",
        "scalability": "string", "cost": "number", "budget": "number",
        # Contact
        "website": "string", "email": "string", "phone": "string",
        "address": "string", "country": "string", "city": "string",
        # Miscellaneous
        "notes": "string", "remarks": "string", "dependencies": "json",
        "prerequisites": "json", "related_document": "string",
        "reference": "string", "active": "boolean",
    }
)

# ==========================================================================
# RELATIONS — reference vocabulary only (relation.relation_type has no
# CHECK constraint in schema.md, so this is advisory normalization, not
# a hard validation gate).
# ==========================================================================

RELATION_TYPE_VOCABULARY: tuple[str, ...] = (
    "uses", "depends_on", "implements", "belongs_to", "managed_by",
    "owned_by", "created_by", "approved_by", "integrates_with", "contains",
    "requires", "supports", "deployed_on", "stored_in", "hosted_on",
    "communicates_with", "related_to",
)

RELATION_TYPE_SYNONYMS: Mapping[str, str] = MappingProxyType(
    {
        "depends on": "depends_on",
        "belongs to": "belongs_to",
        "part of": "belongs_to",
        "managed by": "managed_by",
        "owned by": "owned_by",
        "created by": "created_by",
        "approved by": "approved_by",
        "integrates with": "integrates_with",
        "requires": "requires",
        "runs on": "deployed_on",
        "deployed on": "deployed_on",
        "stored in": "stored_in",
        "hosted on": "hosted_on",
        "talks to": "communicates_with",
        "connects to": "communicates_with",
        "communicates with": "communicates_with",
        "related to": "related_to",
    }
)

# ==========================================================================
# Normalization + private lookup indices
# ==========================================================================


def _normalize(text: str) -> str:
    """Lowercase, collapse hyphens/underscores to spaces, collapse
    repeated whitespace. Pure, total, no I/O."""
    return " ".join(text.strip().lower().replace("-", " ").replace("_", " ").split())


_ENTITY_TYPE_EXACT_INDEX: Mapping[str, str] = MappingProxyType(
    {
        **{_normalize(entity_type): entity_type for entity_type in ALL_ENTITY_TYPES},
        **{_normalize(synonym): canonical for synonym, canonical in ENTITY_TYPE_SYNONYMS.items()},
    }
)

_RELATION_TYPE_EXACT_INDEX: Mapping[str, str] = MappingProxyType(
    {
        **{_normalize(relation_type): relation_type for relation_type in RELATION_TYPE_VOCABULARY},
        **{_normalize(synonym): canonical for synonym, canonical in RELATION_TYPE_SYNONYMS.items()},
    }
)

_FUZZY_MATCH_CUTOFF: float = 0.6
_FUZZY_MATCH_COUNT: int = 3


def _best_fuzzy_matches(normalized_candidate: str, index: Mapping[str, str]) -> tuple[str, ...]:
    """Resolve the closest canonical terms for a normalized candidate
    against a normalized-key index, deduplicated and order-preserved."""
    close_keys = difflib.get_close_matches(
        normalized_candidate, index.keys(), n=_FUZZY_MATCH_COUNT, cutoff=_FUZZY_MATCH_CUTOFF
    )
    return tuple(dict.fromkeys(index[key] for key in close_keys))


def _fuzzy_confidence(normalized_candidate: str, closest_key: str) -> float:
    return round(difflib.SequenceMatcher(None, normalized_candidate, closest_key).ratio(), 2)


# ==========================================================================
# PUBLIC API
# ==========================================================================


def attributes_for_entity_type(entity_type: str) -> tuple[str, ...]:
    """The canonical attribute set for a canonical entity type.

    Raises UnknownEntityTypeError if `entity_type` is not itself a
    canonical entity type (call canonicalize_entity_type() first if the
    text may be raw user wording)."""
    try:
        return ENTITY_TYPE_ATTRIBUTES[entity_type]
    except KeyError as exc:
        raise UnknownEntityTypeError(
            message=f"{entity_type!r} is not a canonical entity type",
            candidate_text=entity_type,
        ) from exc


def value_type_for_attribute(attribute: str) -> ValueType:
    """The CHECK-constraint-compliant value_type for a canonical
    attribute name.

    Raises UnknownAttributeError if `attribute` is not a recognized
    canonical attribute name."""
    try:
        return ATTRIBUTE_VALUE_TYPES[attribute]
    except KeyError as exc:
        raise UnknownAttributeError(
            message=f"no value_type mapping for attribute {attribute!r}",
            candidate_text=attribute,
        ) from exc


def canonicalize_entity_type(candidate_text: str) -> CanonicalizationResult:
    """Normalize raw user wording (abbreviation, synonym, plural,
    conversational reference) into a canonical `entity.entity_type`.

    Raises UnknownEntityTypeError if no canonical entity type is close
    enough to be a plausible match — the extractor must never invent a
    new entity type when this happens."""
    normalized = _normalize(candidate_text)

    if normalized in _ENTITY_TYPE_EXACT_INDEX:
        return CanonicalizationResult(
            canonical_term=_ENTITY_TYPE_EXACT_INDEX[normalized], confidence=1.0
        )

    close_keys = difflib.get_close_matches(
        normalized, _ENTITY_TYPE_EXACT_INDEX.keys(), n=_FUZZY_MATCH_COUNT, cutoff=_FUZZY_MATCH_CUTOFF
    )
    if not close_keys:
        raise UnknownEntityTypeError(
            message=f"no canonical entity type found for {candidate_text!r}",
            candidate_text=candidate_text,
        )

    resolved = tuple(dict.fromkeys(_ENTITY_TYPE_EXACT_INDEX[key] for key in close_keys))
    best, *alternates = resolved
    return CanonicalizationResult(
        canonical_term=best,
        confidence=_fuzzy_confidence(normalized, close_keys[0]),
        alternate_candidates=tuple(alternates),
    )


def canonicalize_attribute(entity_type: str, candidate_text: str) -> CanonicalizationResult:
    """Normalize raw user wording into a canonical attribute name that is
    valid *for the given entity type* — attributes are scoped per entity
    type, so a term must resolve within `entity_type`'s own attribute set.

    Raises UnknownEntityTypeError if `entity_type` itself isn't canonical.
    Raises UnknownAttributeError if no attribute of that entity type is
    close enough to be a plausible match."""
    valid_attributes = attributes_for_entity_type(entity_type)
    normalized = _normalize(candidate_text)

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

    if normalized in scoped_index:
        return CanonicalizationResult(canonical_term=scoped_index[normalized], confidence=1.0)

    close_keys = difflib.get_close_matches(
        normalized, scoped_index.keys(), n=_FUZZY_MATCH_COUNT, cutoff=_FUZZY_MATCH_CUTOFF
    )
    if not close_keys:
        raise UnknownAttributeError(
            message=(
                f"no canonical attribute found for {candidate_text!r} "
                f"on entity type {entity_type!r}"
            ),
            entity_type=entity_type,
            candidate_text=candidate_text,
        )

    resolved = tuple(dict.fromkeys(scoped_index[key] for key in close_keys))
    best, *alternates = resolved
    return CanonicalizationResult(
        canonical_term=best,
        confidence=_fuzzy_confidence(normalized, close_keys[0]),
        alternate_candidates=tuple(alternates),
    )


def canonicalize_relation_type(candidate_text: Optional[str]) -> Optional[CanonicalizationResult]:
    """Normalize raw user wording into a relation_type.

    Unlike entity types and attributes, `relation.relation_type` carries
    no CHECK constraint in schema.md — so this function never raises for
    an unrecognized term. A vocabulary/synonym match returns high
    confidence; a fuzzy match returns proportional confidence; a
    complete miss still returns a slugified version of the candidate
    text at low confidence, since a free-text relation is valid schema-
    wise even when it isn't a recognized vocabulary term. Returns None
    only when no relation was expressed at all."""
    if candidate_text is None or not candidate_text.strip():
        return None

    normalized = _normalize(candidate_text)

    if normalized in _RELATION_TYPE_EXACT_INDEX:
        return CanonicalizationResult(
            canonical_term=_RELATION_TYPE_EXACT_INDEX[normalized], confidence=1.0
        )

    close_keys = difflib.get_close_matches(
        normalized, _RELATION_TYPE_EXACT_INDEX.keys(), n=_FUZZY_MATCH_COUNT, cutoff=_FUZZY_MATCH_CUTOFF
    )
    if close_keys:
        resolved = tuple(dict.fromkeys(_RELATION_TYPE_EXACT_INDEX[key] for key in close_keys))
        best, *alternates = resolved
        return CanonicalizationResult(
            canonical_term=best,
            confidence=_fuzzy_confidence(normalized, close_keys[0]),
            alternate_candidates=tuple(alternates),
        )

    return CanonicalizationResult(canonical_term=normalized.replace(" ", "_"), confidence=0.30)


# ==========================================================================
# Module-load-time totality check: every attribute referenced by any
# entity type must have a value_type mapping, or the CHECK constraint
# would be silently violated the first time that attribute is written.
# Fails fast at import time rather than at first structured write.
# ==========================================================================

_ALL_REFERENCED_ATTRIBUTES: frozenset[str] = frozenset(
    attribute
    for attributes in ENTITY_TYPE_ATTRIBUTES.values()
    for attribute in attributes
)
_MISSING_VALUE_TYPES: frozenset[str] = _ALL_REFERENCED_ATTRIBUTES - frozenset(ATTRIBUTE_VALUE_TYPES.keys())
if _MISSING_VALUE_TYPES:
    raise AssertionError(
        f"ontology.py is inconsistent: attributes {sorted(_MISSING_VALUE_TYPES)} are used in "
        "ENTITY_TYPE_ATTRIBUTES but have no entry in ATTRIBUTE_VALUE_TYPES"
    )
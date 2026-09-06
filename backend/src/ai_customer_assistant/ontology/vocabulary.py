"""
vocabulary.py — the single source of truth for the project's ontology.

Two levels, only one of which is persisted:

  Domain (application-level grouping only — NOT persisted; exists purely
  to help humans and the extractor organize Entity Types, has no
  representation in the database whatsoever)
      -> Entity Type (persisted — maps directly to `entity.entity_type`,
         VARCHAR(64); this is the only level ever written to or read
         from the database)

Attributes are NOT a shared namespace: `attribute.namespace` is scoped per
entity type, and `attribute.name` is only unique *within* that namespace
(composite unique constraint on `(namespace, name)`). So
`ENTITY_TYPE_ATTRIBUTES` below is one dispatch table per entity type, not a
single flat attribute set — two entity types are free to each define their
own `status` independently.

## Why this module exists

The ingestion extractor and the Knowledge Agent used to carry *forked
copies* of these tables — roughly 750 lines each, kept in sync by hand. The
fork was deliberate (ingestion was not to depend on the agents package) but
the copies drifted, and the failure mode is silent:

  * ingestion canonicalized "data engineer" to `Person`; the retrieval side
    could not canonicalize that wording at all, so a question phrased that
    way matched nothing;
  * ingestion canonicalized "mobile developer" to `Person`, while retrieval
    fuzzy-matched it to `Mobile App` at 0.88 confidence — high enough to be
    trusted, and wrong;
  * ingestion allowed `salary` on `Person`, so it could write a salary fact
    that retrieval was structurally unable to ask for.

None of that raises an error anywhere. It just quietly loses facts.

This module is neutral ground: it belongs to neither package, so both can
import it without either depending on the other — which preserves the reason
the fork existed while removing the duplication that made drift possible.
`agents/knowledge/ontology.py` and `ingestion/extraction/ontology.py` are now
thin adapters over `ontology.matching`, keeping their own exception types and
result shapes. The tables live here, once.

Adding a term? Add it here and both sides get it.
"""

from __future__ import annotations

import difflib
from types import MappingProxyType
from typing import Literal, Mapping

# Mirrors the `attribute.value_type` CHECK constraint.
ValueType = Literal["string", "number", "boolean", "date", "json"]

# ==========================================================================
# DOMAIN -> ENTITY TYPE (application-level grouping only, never persisted)
# ==========================================================================

DOMAIN_ENTITY_TYPES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "Organization": (
            "Company", "Department", "Team", "Office", "Employee", "Person",
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
            "Employee Policy", "Leave Policy",
        ),
        "Company Services": (
            "Web Development", "Mobile App", "AI System", "Frontend",
            "Backend", "Animation", "UI/UX Design",
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

ALL_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type
    for entity_types in DOMAIN_ENTITY_TYPES.values()
    for entity_type in entity_types
)

# Reverse map, kept for documentation / confidence scoring only — the Domain
# level is never written to or read from `entity.entity_type`.
ENTITY_TYPE_TO_DOMAIN: Mapping[str, str] = MappingProxyType(
    {
        entity_type: domain
        for domain, entity_types in DOMAIN_ENTITY_TYPES.items()
        for entity_type in entity_types
    }
)

# --------------------------------------------------------------------------
# Entity-type synonyms — informal/abbreviated user wording -> canonical
# entity type. Matched after normalization (see _normalize below). Includes
# the agent ontology's synonyms plus the ingestion-specific mappings that
# fix the observed duplicate ("organization" -> "Company", etc.).
# --------------------------------------------------------------------------

ENTITY_TYPE_SYNONYMS: Mapping[str, str] = MappingProxyType(
    {
        # -- agent ontology synonyms --------------------------------------
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
        # -- ingestion-specific: dedup the observed type variants ---------
        "company": "Company",
        "organization": "Company",
        "organisation": "Company",
        "corporation": "Company",
        "firm": "Company",
        "startup": "Company",
        "person": "Person",
        "people": "Person",
        "individual": "Person",
        "employee": "Employee",
        "staff": "Employee",
        "team member": "Employee",
        "sector": "Industry",
        "industry": "Industry",
        "market segment": "Industry",
        "non-profit": "Industry",
        "nonprofits": "Industry",
        "non profits": "Industry",
        "technology": "Technology",
        "service": "Service",
        "consulting": "Consulting Service",
        "product": "Product",
        "project": "Project",
        # -- company services (web / mobile / AI / design / animation) ------
        "web development": "Web Development",
        "web dev": "Web Development",
        "web development service": "Web Development",
        "website development": "Web Development",
        "web development services": "Web Development",
        "mobile app": "Mobile App",
        "mobile app development": "Mobile App",
        "mobile application": "Mobile App",
        "mobile development": "Mobile App",
        "mobile developer": "Person",
        "data engineer": "Person",
        "mobile app development services": "Mobile App",
        "ios development": "Mobile App",
        "android development": "Mobile App",
        "app development": "Mobile App",
        "ai system": "AI System",
        "ai systems": "AI System",
        "ai development": "AI System",
        "ai development services": "AI System",
        "artificial intelligence system": "AI System",
        "ai solution": "AI System",
        "frontend": "Frontend",
        "front-end": "Frontend",
        "front end": "Frontend",
        "frontend development": "Frontend",
        "frontend development services": "Frontend",
        "backend": "Backend",
        "back-end": "Backend",
        "back end": "Backend",
        "backend development": "Backend",
        "backend development services": "Backend",
        "animation": "Animation",
        "animation service": "Animation",
        "animation services": "Animation",
        "motion graphics": "Animation",
        "2d animation": "Animation",
        "3d animation": "Animation",
        "ui ux": "UI/UX Design",
        "ui/ux": "UI/UX Design",
        "ui/ux design": "UI/UX Design",
        "ui design": "UI/UX Design",
        "ux design": "UI/UX Design",
        "ux": "UI/UX Design",
        "web design": "UI/UX Design",
        # -- company policies / HR ------------------------------------------
        "employee policy": "Employee Policy",
        "employee policies": "Employee Policy",
        "hr policy": "Employee Policy",
        "human resources policy": "Employee Policy",
        "staff policy": "Employee Policy",
        "leave policy": "Leave Policy",
        "leave policies": "Leave Policy",
        "vacation policy": "Leave Policy",
        "time off policy": "Leave Policy",
        "annual leave policy": "Leave Policy",
        "holiday policy": "Leave Policy",
    }
)

# ==========================================================================
# ENTITY TYPE -> ATTRIBUTES (schema-scoped: attribute.namespace = entity type)
# ==========================================================================

_BASE_ENTITY_ATTRIBUTES: tuple[str, ...] = (
    "name", "description", "status", "tags", "created_at", "updated_at",
)

_ENTITY_TYPE_ATTRIBUTE_EXTENSIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        # -- Organization ---------------------------------------------------
        "Company": ("industry", "website", "country", "city"),
        "Department": ("owner", "team"),
        "Team": ("manager", "department"),
        "Office": ("country", "city", "address"),
        "Employee": ("role", "designation", "department", "email", "phone", "salary", "joined_on"),
        "Person": ("role", "title", "employer", "department", "email", "phone", "salary"),
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
        "Package": ("pricing_model", "version", "cost"),
        "Proposal": ("target_customer", "estimated_duration", "cost"),
        "Product": ("category", "version", "pricing_model"),
        "Project": ("owner", "start_date", "end_date", "budget"),
        "Feature": ("version", "dependencies"),
        "Module": ("version", "dependencies", "programming_language"),
        "Component": ("version", "dependencies", "technology"),
        "API": ("api_type", "protocol", "authentication_method", "version"),
        "Microservice": ("technology", "programming_language", "protocol", "deployment_model"),
        "Integration": ("provider", "authentication_method", "protocol"),
        # -- Company Services ------------------------------------------------
        "Web Development": ("stack", "frameworks", "portfolio", "pricing_model", "turnaround"),
        "Mobile App": ("platform", "os", "frameworks", "portfolio", "pricing_model"),
        "AI System": ("ai_application", "models_used", "integration", "pricing_model"),
        "Frontend": ("frameworks", "technologies", "pricing_model"),
        "Backend": ("frameworks", "technologies", "api", "pricing_model"),
        "Animation": ("animation_type", "tools", "portfolio", "pricing_model", "turnaround"),
        "UI/UX Design": ("design_tools", "portfolio", "pricing_model"),
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
        "Employee Policy": ("category", "effective_date", "review_date", "department", "owner"),
        "Leave Policy": ("category", "effective_date", "review_date", "leave_types", "accrual"),
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
        "job title": "title",
        "company": "employer",
        "organisation": "employer",
        "organization": "employer",
        "works at": "employer",
        "field": "category",
        "segment": "category",
        "price": "cost",
        "rate": "cost",
        "price range": "cost",
        "monthly rate": "cost",
        "fee": "cost",
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
        "tags": "json", "role": "string", "employer": "string",
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
        # Company Services
        "stack": "string", "frameworks": "json", "portfolio": "string",
        "turnaround": "string", "platform": "string", "os": "string",
        "ai_application": "string", "models_used": "json", "integration": "string",
        "technologies": "json", "api": "string", "animation_type": "string",
        "tools": "json", "design_tools": "json",
        # HR / policies
        "designation": "string", "salary": "number", "joined_on": "date",
        "leave_types": "json", "accrual": "string",
        # Miscellaneous
        "notes": "string", "remarks": "string", "dependencies": "json",
        "prerequisites": "json", "related_document": "string",
        "reference": "string", "active": "boolean",
    }
)

# ==========================================================================
# RELATIONS — advisory vocabulary (relation.relation_type has no CHECK
# constraint in schema.md, so this is normalization, not a hard gate).
# ==========================================================================

RELATION_TYPE_VOCABULARY: tuple[str, ...] = (
    "uses", "depends_on", "implements", "belongs_to", "managed_by",
    "owned_by", "created_by", "approved_by", "integrates_with", "contains",
    "requires", "supports", "deployed_on", "stored_in", "hosted_on",
    "communicates_with", "related_to", "employs", "provides", "leads",
    "targets", "serves", "founded_by", "specializes_in", "delivers",
    "builds", "develops", "designs",
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
        "employs": "employs",
        "works for": "employs",
        "works at": "employs",
        "is employed by": "employs",
        "hires": "employs",
        "provides": "provides",
        "offers": "provides",
        "sells": "provides",
        "targets": "targets",
        "serves": "serves",
        "leads": "leads",
        "headed by": "leads",
        "founded by": "founded_by",
        "founder of": "founded_by",
        "specializes in": "specializes_in",
        "specialised in": "specializes_in",
        "delivers": "delivers",
        "builds": "builds",
        "develops": "develops",
        "designs": "designs",
    }
)


# ==========================================================================
# Normalization + private lookup indices
# ==========================================================================

_FUZZY_MATCH_CUTOFF: float = 0.6
_FUZZY_MATCH_COUNT: int = 3


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


def _fuzzy_confidence(normalized_candidate: str, closest_key: str) -> float:
    return round(difflib.SequenceMatcher(None, normalized_candidate, closest_key).ratio(), 2)



# ==========================================================================
# Module-load-time totality check: every attribute referenced by any entity
# type must have a value_type mapping, or the CHECK constraint would be
# silently violated the first time that attribute is written. Fails fast at
# import rather than at the first structured write.
# ==========================================================================

_ALL_REFERENCED_ATTRIBUTES: frozenset[str] = frozenset(
    attribute
    for attributes in ENTITY_TYPE_ATTRIBUTES.values()
    for attribute in attributes
)
_MISSING_VALUE_TYPES: frozenset[str] = _ALL_REFERENCED_ATTRIBUTES - frozenset(
    ATTRIBUTE_VALUE_TYPES.keys()
)
if _MISSING_VALUE_TYPES:
    raise AssertionError(
        f"ontology vocabulary is inconsistent: attributes {sorted(_MISSING_VALUE_TYPES)} are "
        "used in ENTITY_TYPE_ATTRIBUTES but have no entry in ATTRIBUTE_VALUE_TYPES"
    )

"""
Data layer for the EAV extraction agent: the pydantic models LangChain uses
to generate tool-call JSON schemas, plus the small closed vocabulary the
model must pick from. Kept separate from tools.py (which is the I/O/compute
layer that actually executes a validated call).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

VALUE_TYPES = ("string", "number", "boolean", "date", "json")
ValueType = Literal["string", "number", "boolean", "date", "json"]


class ResolveEntityArgs(BaseModel):
    """Resolve-or-create an entity, per entity(entity_type, name) uniqueness."""

    entity_type: str = Field(
        ...,
        description="The entity's type. Use a canonical entity type from the vocabulary in the system prompt (e.g. 'Company', 'Person', 'Service', 'Project', 'Industry') — not a synonym or case variant.",
    )
    name: str = Field(..., description="The entity's display name as it appears in the text")
    label: str = Field(..., description="Short human-readable label for the entity")


class RecordAttributeValueArgs(BaseModel):
    """Attach one fact to a previously-resolved entity."""

    entity_type: str
    entity_name: str
    namespace: str = Field(..., description="Groups related attributes, e.g. 'company'")
    attribute_name: str = Field(
        ...,
        description="Canonical attribute name valid for the entity type (e.g. 'industry', 'website', 'role', 'status') — not a synonym or case variant.",
    )
    value: str = Field(..., description="The raw value; interpreted per value_type")
    value_type: ValueType = "string"
    multivalue: bool = False
    searchable: bool = True


class RecordRelationArgs(BaseModel):
    """Connect two previously-resolved entities."""

    source_entity_type: str
    source_entity_name: str
    target_entity_type: str
    target_entity_name: str
    relation_type: str = Field(
        ...,
        description="Canonical relation type (e.g. 'uses', 'employs', 'provides', 'leads') — not a synonym or case variant.",
    )


class NoFactFound(BaseModel):
    """The agent calls this when a chunk contains no extractable fact."""

    reason: str = "no concrete entity/fact found in this chunk"

"""
structured_lookup.py — deterministic retrieval from the EAV schema.

The only module in the Knowledge Agent package that reads
`entity` / `attribute` / `value` / `relation` directly — every other
module receives its data already resolved. No vector search, no LLM;
pure deterministic reads against the schema described in schema.md.

Schema note on `entity.label` vs `entity.name`: schema.md defines both
columns, but only `(entity_type, name)` carries the composite-unique
dedup constraint — `name` is therefore the identifying text an
extracted `entity_label` (e.g. "Project Alpha") should match against.
`entity.label`'s further semantics aren't specified beyond its type, so
this module doesn't read or write it; only `entity.name` is surfaced as
`StructuredFact.entity_label` / matched against
`StructuredQuery.entity_label`.

I/O boundary: `_fetch_rows()` is the only place this module calls
`session.execute()`. Everything that builds a `Select` statement is a
pure function — testable by inspecting the statement object without a
database at all; the async round-trip is exercised separately against
a real in-memory SQLite database (aiosqlite), matching the
`ingestion/storage` convention of fakes/sqlite over mocks.
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, MetaData, String, Table, func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from .exceptions import AmbiguousEntityError, AttributeNotFoundError, EntityNotFoundError
from .types import StructuredFact, StructuredQuery

# ==========================================================================
# Data layer — structural mirror of schema.md's EAV tables. schema.md
# remains the authoritative source; this is only enough structure to
# build correct Select statements against it.
# ==========================================================================

metadata = MetaData()

entity_table = Table(
    "entity",
    metadata,
    Column("id", String, primary_key=True),
    Column("label", String, nullable=False),
    Column("entity_type", String, nullable=False),
    Column("name", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
)

attribute_table = Table(
    "attribute",
    metadata,
    Column("id", String, primary_key=True),
    Column("namespace", String, nullable=False),
    Column("name", String, nullable=False),
    Column("value_type", String, nullable=False),
    Column("multivalue", Boolean, nullable=False),
)

value_table = Table(
    "value",
    metadata,
    Column("id", String, primary_key=True),
    Column("entity_id", String, ForeignKey("entity.id"), nullable=False),
    Column("attribute_id", String, ForeignKey("attribute.id"), nullable=False),
    Column("value", String, nullable=False),
    Column("searchable", Boolean, nullable=False),
    Column("created_at", DateTime, nullable=False),
)

relation_table = Table(
    "relation",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_entity_id", String, ForeignKey("entity.id"), nullable=False),
    Column("target_entity_id", String, ForeignKey("entity.id"), nullable=False),
    Column("relation_type", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


# ==========================================================================
# Public API
# ==========================================================================


async def structured_lookup(query: StructuredQuery, *, session: AsyncSession) -> tuple[StructuredFact, ...]:
    """Deterministic EAV lookup for a canonicalized StructuredQuery.

    Returns an empty tuple (never None) when `query.entity_type` is
    unset — there's nothing structured to look up, which is a valid
    outcome, not a failure. Also returns an empty tuple when the
    resolved entity has no matching values/relations for a *general*
    lookup (no specific attribute or relation was requested).

    Raises EntityNotFoundError if no entity matches (entity_type,
    entity_label). Raises AmbiguousEntityError if more than one does.
    Raises AttributeNotFoundError if a *specific* attribute was
    requested and the resolved entity has no value for it."""
    if query.entity_type is None:
        return ()

    entity_row = await _resolve_single_entity(query, session=session)
    lookup_kind = _lookup_kind(query)
    handler = _LOOKUP_DISPATCH[lookup_kind]
    return await handler(entity_row, query, session)


# ==========================================================================
# Internals — entity resolution
# ==========================================================================


def _entity_lookup_statement(query: StructuredQuery) -> Select:
    """Pure: build (never execute) the entity-resolution statement."""
    stmt = select(entity_table.c.id, entity_table.c.entity_type, entity_table.c.name).where(
        entity_table.c.entity_type == query.entity_type
    )
    if query.entity_label is not None:
        stmt = stmt.where(func.lower(entity_table.c.name) == query.entity_label.lower())
    return stmt


async def _resolve_single_entity(query: StructuredQuery, *, session: AsyncSession) -> Row:
    rows = await _fetch_rows(session, _entity_lookup_statement(query))
    if len(rows) == 0:
        raise EntityNotFoundError(
            message=f"no entity found for entity_type={query.entity_type!r}, entity_label={query.entity_label!r}",
            entity_type=query.entity_type,
            entity_label=query.entity_label or "",
        )
    if len(rows) > 1:
        raise AmbiguousEntityError(
            message=(
                f"{len(rows)} entities match entity_type={query.entity_type!r}, "
                f"entity_label={query.entity_label!r} — need a more specific label"
            ),
            entity_type=query.entity_type,
            entity_label=query.entity_label or "",
            candidate_count=len(rows),
        )
    (single_row,) = rows
    return single_row


# ==========================================================================
# Internals — lookup-kind dispatch (relation / attribute / general)
# ==========================================================================


def _lookup_kind(query: StructuredQuery) -> str:
    """Pure: which of the three lookup shapes applies. A relation
    request takes priority over an attribute request since extraction.py
    only ever populates one of the two for a given query."""
    return next(
        kind
        for kind, is_applicable in (
            ("relation", query.relation_type is not None),
            ("attribute", query.attribute is not None),
            ("general", True),
        )
        if is_applicable
    )


def _attribute_lookup_statement(entity_id: str, entity_type: str, attribute_name: str) -> Select:
    return (
        select(value_table.c.value, attribute_table.c.value_type)
        .select_from(value_table.join(attribute_table, value_table.c.attribute_id == attribute_table.c.id))
        .where(value_table.c.entity_id == entity_id)
        .where(attribute_table.c.namespace == entity_type)
        .where(attribute_table.c.name == attribute_name)
    )


async def _attribute_lookup(entity_row: Row, query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    rows = await _fetch_rows(
        session, _attribute_lookup_statement(entity_row.id, entity_row.entity_type, query.attribute)
    )
    if not rows:
        raise AttributeNotFoundError(
            message=(
                f"entity {entity_row.name!r} ({entity_row.entity_type}) has no value "
                f"for attribute {query.attribute!r}"
            ),
            entity_type=entity_row.entity_type,
            entity_label=entity_row.name,
            attribute=query.attribute,
        )
    return tuple(
        StructuredFact(
            entity_id=entity_row.id,
            entity_type=entity_row.entity_type,
            entity_label=entity_row.name,
            attribute=query.attribute,
            value=row.value,
            value_type=row.value_type,
        )
        for row in rows
    )


def _general_values_statement(entity_id: str) -> Select:
    return (
        select(attribute_table.c.name.label("attribute_name"), value_table.c.value, attribute_table.c.value_type)
        .select_from(value_table.join(attribute_table, value_table.c.attribute_id == attribute_table.c.id))
        .where(value_table.c.entity_id == entity_id)
    )


async def _general_lookup(entity_row: Row, query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    rows = await _fetch_rows(session, _general_values_statement(entity_row.id))
    return tuple(
        StructuredFact(
            entity_id=entity_row.id,
            entity_type=entity_row.entity_type,
            entity_label=entity_row.name,
            attribute=row.attribute_name,
            value=row.value,
            value_type=row.value_type,
        )
        for row in rows
    )


def _relation_lookup_statement(entity_id: str, relation_type: str) -> Select:
    target = entity_table.alias("target_entity")
    return (
        select(
            relation_table.c.relation_type,
            target.c.name.label("target_name"),
            target.c.entity_type.label("target_type"),
        )
        .select_from(relation_table.join(target, relation_table.c.target_entity_id == target.c.id))
        .where(relation_table.c.source_entity_id == entity_id)
        .where(relation_table.c.relation_type == relation_type)
    )


async def _relation_lookup(entity_row: Row, query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    rows = await _fetch_rows(session, _relation_lookup_statement(entity_row.id, query.relation_type))
    return tuple(
        StructuredFact(
            entity_id=entity_row.id,
            entity_type=entity_row.entity_type,
            entity_label=entity_row.name,
            attribute=row.relation_type,
            value=row.target_name,
            value_type="string",
            related_entity_label=row.target_name,
            relation_type=row.relation_type,
        )
        for row in rows
    )


_LOOKUP_DISPATCH: Mapping[str, Callable] = {
    "relation": _relation_lookup,
    "attribute": _attribute_lookup,
    "general": _general_lookup,
}


# ==========================================================================
# Internals — I/O boundary (the only place this module executes SQL)
# ==========================================================================


async def _fetch_rows(session: AsyncSession, stmt: Select) -> Sequence[Row]:
    result = await session.execute(stmt)
    return result.all()
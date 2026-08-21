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

from typing import Callable, Mapping, Optional, Sequence

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, MetaData, String, Table, func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from .exceptions import AttributeNotFoundError, EntityNotFoundError
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


# Membership / container relations. A "who are the members of X?" style
# query canonicalizes to one of these (e.g. "members" -> the vocabulary
# term "contains"), but the ingested graph stores membership across many
# role relations ("advisory board member", "founded_by", "project_manager",
# "sr_software_engineer", ...) rather than a single "member" edge. Exact
# matching on one relation type therefore finds nothing for such queries,
# so when a membership-type relation lookup comes up empty we degrade to a
# membership lookup that surfaces the entity's person-targeted relations
# (its roster), falling back to the full relation set only when no people
# are linked.
#
# "related_to" is also included: it is the generic catch-all the extractor
# falls back to when it can't pin a role relation to the vocabulary
# ("who are the advisors of X?" -> "related_to"), so treating it the same
# way keeps people-role questions ("advisors", "board members", ...)
# answerable instead of dead-ending on an empty exact match.
_MEMBERSHIP_RELATION_TYPES: frozenset[str] = frozenset(
    {
        "contains",
        "includes",
        "includes member",
        "members",
        "member",
        "has member",
        "has_member",
        "member of",
        "member_of",
        "comprises",
        "made up of",
        "team",
        "team member",
        "team_member",
        "related to",
        "related_to",
    }
)

# Entity types whose instances count as people when assembling a member
# roster from an organization's relations (matched case-insensitively).
_PERSON_ENTITY_TYPES: frozenset[str] = frozenset(
    {"person", "people", "employee", "member", "staff", "personnel"}
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

    Entity resolution is merge-aware: when several `entity` rows match
    the same label under different entity-type spellings (ingestion has
    historically stored the same person as both `person` and `Person`,
    splitting relations between the two), their values and relations are
    UNIONED rather than failing as ambiguous — a label uniquely names a
    real-world entity even when its stored type strings don't agree.

    Raises EntityNotFoundError if no entity matches (entity_type,
    entity_label). Raises AttributeNotFoundError if a *specific*
    attribute was requested and the resolved entity has no value for
    it."""
    if query.entity_type is None:
        return ()

    entity_rows = await _resolve_entities(query, session=session)
    lookup_kind = _lookup_kind(query)
    handler = _LOOKUP_DISPATCH[lookup_kind]
    return await handler(entity_rows, query, session)


# ==========================================================================
# Internals — entity resolution
# ==========================================================================


def _entity_lookup_statement(query: StructuredQuery) -> Select:
    """Pure: build (never execute) the entity-resolution statement.

    ``entity_type`` and ``entity_label`` are matched case-insensitively so
    a canonical ontology term from extraction ("Employee") still resolves
    an entity that ingestion stored under a differently-cased type
    ("person")."""
    stmt = select(entity_table.c.id, entity_table.c.entity_type, entity_table.c.name)
    if query.entity_type is not None:
        stmt = stmt.where(func.lower(entity_table.c.entity_type) == query.entity_type.lower())
    if query.entity_label is not None:
        stmt = stmt.where(func.lower(entity_table.c.name) == query.entity_label.lower())
    return stmt


def _label_only_statement(entity_label: str) -> Select:
    """Pure: build an entity-resolution statement matching by label alone,
    used as a fallback when the ontology's entity-type vocabulary doesn't
    line up with what ingestion actually stored."""
    return select(entity_table.c.id, entity_table.c.entity_type, entity_table.c.name).where(
        func.lower(entity_table.c.name) == entity_label.lower()
    )


def _entity_ids(entity_rows: Sequence[Row]) -> tuple[str, ...]:
    """Pure: the ids of every resolved entity row."""
    return tuple(row.id for row in entity_rows)


def _queried_entity(entity_rows: Sequence[Row]) -> Row:
    """Pure: the display entity for facts oriented on the queried entity.

    Multiple rows are type variants of the same real-world entity (they
    share `name`), so the first row's identity stands in for all of
    them."""
    return entity_rows[0]


async def _resolve_entities(query: StructuredQuery, *, session: AsyncSession) -> tuple[Row, ...]:
    rows = await _fetch_rows(session, _entity_lookup_statement(query))
    if not rows and query.entity_label is not None and query.entity_type is not None:
        # Ontology/ingestion type-vocabulary mismatch (e.g. extraction
        # canonicalizes to "Employee" but the entity was stored as
        # "person"): retry by label alone, which is type-independent.
        rows = await _fetch_rows(session, _label_only_statement(query.entity_label))
    if len(rows) == 0:
        raise EntityNotFoundError(
            message=f"no entity found for entity_type={query.entity_type!r}, entity_label={query.entity_label!r}",
            entity_type=query.entity_type,
            entity_label=query.entity_label or "",
        )
    return tuple(rows)


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


def _attribute_lookup_statement(entity_ids: Sequence[str], attribute_name: str) -> Select:
    return (
        select(value_table.c.value, attribute_table.c.value_type)
        .select_from(value_table.join(attribute_table, value_table.c.attribute_id == attribute_table.c.id))
        .where(value_table.c.entity_id.in_(entity_ids))
        .where(attribute_table.c.name == attribute_name)
    )


async def _attribute_lookup(entity_rows: Sequence[Row], query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    rows = await _fetch_rows(
        session, _attribute_lookup_statement(_entity_ids(entity_rows), query.attribute)
    )
    if not rows:
        # No stored value for the requested attribute — but the question may
        # still be answerable from the entity's relations (roles are
        # commonly stored as relations, e.g. "Alpinist Studios
        # --account_officer--> Devin Rajkarnikar"). Degrade to a general
        # lookup so "what is X's role?" isn't a dead end; only report "no
        # value" when the entity has nothing at all.
        general = await _general_lookup(entity_rows, query, session)
        if general:
            return general
        entity = _queried_entity(entity_rows)
        raise AttributeNotFoundError(
            message=(
                f"entity {query.entity_label!r} has no value for attribute {query.attribute!r}"
            ),
            entity_type=query.entity_type or entity.entity_type,
            entity_label=query.entity_label or entity.name,
            attribute=query.attribute,
        )
    entity = _queried_entity(entity_rows)
    entity_type = query.entity_type or entity.entity_type
    entity_label = query.entity_label or entity.name
    return tuple(
        StructuredFact(
            entity_id=entity.id,
            entity_type=entity_type,
            entity_label=entity_label,
            attribute=query.attribute,
            value=row.value,
            value_type=row.value_type,
        )
        for row in rows
    )


def _general_values_statement(entity_ids: Sequence[str]) -> Select:
    return (
        select(attribute_table.c.name.label("attribute_name"), value_table.c.value, attribute_table.c.value_type)
        .select_from(value_table.join(attribute_table, value_table.c.attribute_id == attribute_table.c.id))
        .where(value_table.c.entity_id.in_(entity_ids))
    )


def _outgoing_relations_statement(
    entity_ids: Sequence[str], relation_type: Optional[str] = None
) -> Select:
    target = entity_table.alias("target_entity")
    stmt = (
        select(
            relation_table.c.relation_type,
            target.c.name.label("target_name"),
            target.c.entity_type.label("target_type"),
        )
        .select_from(relation_table.join(target, relation_table.c.target_entity_id == target.c.id))
        .where(relation_table.c.source_entity_id.in_(entity_ids))
    )
    if relation_type is not None:
        stmt = stmt.where(relation_table.c.relation_type == relation_type)
    return stmt


def _incoming_relations_statement(
    entity_ids: Sequence[str], relation_type: Optional[str] = None
) -> Select:
    source = entity_table.alias("source_entity")
    stmt = (
        select(
            relation_table.c.relation_type,
            source.c.id.label("source_entity_id"),
            source.c.name.label("source_name"),
            source.c.entity_type.label("source_type"),
        )
        .select_from(relation_table.join(source, relation_table.c.source_entity_id == source.c.id))
        .where(relation_table.c.target_entity_id.in_(entity_ids))
    )
    if relation_type is not None:
        stmt = stmt.where(relation_table.c.relation_type == relation_type)
    return stmt


def _outgoing_fact(entity_row: Row, row: Row) -> StructuredFact:
    """Fact oriented on the queried entity: ``entity --rel--> target``."""
    return StructuredFact(
        entity_id=entity_row.id,
        entity_type=entity_row.entity_type,
        entity_label=entity_row.name,
        attribute=row.relation_type,
        value=row.target_name,
        value_type="string",
        related_entity_label=row.target_name,
        relation_type=row.relation_type,
    )


def _is_membership_relation(relation_type: Optional[str]) -> bool:
    """Pure: whether a canonicalized relation type expresses membership /
    containment, in which case an empty exact lookup degrades to a general
    lookup so "who are the members of X?" stays answerable."""
    return relation_type is not None and relation_type.strip().lower() in _MEMBERSHIP_RELATION_TYPES


def _is_person_type(entity_type: Optional[str]) -> bool:
    """Pure: whether an entity type denotes a person (case-insensitive),
    used to filter an organization's relations down to its member roster."""
    return entity_type is not None and entity_type.strip().lower() in _PERSON_ENTITY_TYPES


def _incoming_fact(entity_row: Row, row: Row) -> StructuredFact:
    """Fact oriented on the relation's source so semantics stay correct:
    ``source --rel--> entity`` (e.g. "Alpinist Studios employs Justin
    Flores" stays that way even though Justin Flores is the queried
    entity)."""
    return StructuredFact(
        entity_id=row.source_entity_id,
        entity_type=row.source_type,
        entity_label=row.source_name,
        attribute=row.relation_type,
        value=entity_row.name,
        value_type="string",
        related_entity_label=entity_row.name,
        relation_type=row.relation_type,
    )


async def _general_lookup(entity_rows: Sequence[Row], query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    entity = _queried_entity(entity_rows)
    entity_type = query.entity_type or entity.entity_type
    entity_label = query.entity_label or entity.name
    value_facts = tuple(
        StructuredFact(
            entity_id=entity.id,
            entity_type=entity_type,
            entity_label=entity_label,
            attribute=row.attribute_name,
            value=row.value,
            value_type=row.value_type,
        )
        for row in await _fetch_rows(session, _general_values_statement(_entity_ids(entity_rows)))
    )
    outgoing = await _fetch_rows(session, _outgoing_relations_statement(_entity_ids(entity_rows)))
    incoming = await _fetch_rows(session, _incoming_relations_statement(_entity_ids(entity_rows)))
    return (
        *value_facts,
        *(_outgoing_fact(entity, row) for row in outgoing),
        *(_incoming_fact(entity, row) for row in incoming),
    )


async def _relation_lookup(entity_rows: Sequence[Row], query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    entity = _queried_entity(entity_rows)
    outgoing = await _fetch_rows(
        session, _outgoing_relations_statement(_entity_ids(entity_rows), query.relation_type)
    )
    incoming = await _fetch_rows(
        session, _incoming_relations_statement(_entity_ids(entity_rows), query.relation_type)
    )
    if not outgoing and not incoming and _is_membership_relation(query.relation_type):
        # "members of X" is stored across many role relations, not a single
        # typed edge; surface the entity's person-targeted relations (its
        # roster) rather than reporting an unanswerable empty result.
        return await _membership_lookup(entity_rows, query, session)
    return (
        *(_outgoing_fact(entity, row) for row in outgoing),
        *(_incoming_fact(entity, row) for row in incoming),
    )


async def _membership_lookup(entity_rows: Sequence[Row], query: StructuredQuery, session: AsyncSession) -> tuple[StructuredFact, ...]:
    """Assemble the member roster: the queried entity's relations whose
    counterpart is a person. Falls back to a general lookup when the entity
    has no person-targeted relations, so a membership query never dead-ends
    on an empty result."""
    entity = _queried_entity(entity_rows)
    outgoing = await _fetch_rows(session, _outgoing_relations_statement(_entity_ids(entity_rows)))
    incoming = await _fetch_rows(session, _incoming_relations_statement(_entity_ids(entity_rows)))
    member_facts = (
        *(_outgoing_fact(entity, row) for row in outgoing if _is_person_type(row.target_type)),
        *(_incoming_fact(entity, row) for row in incoming if _is_person_type(row.source_type)),
    )
    if not member_facts:
        return await _general_lookup(entity_rows, query, session)
    return member_facts


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
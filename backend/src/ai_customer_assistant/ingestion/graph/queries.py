"""
Read-only traversal layer over the entity/attribute/value/relation EAV
schema (see db/models.py). This module owns every query the graph API
(api/graph.py, not yet written) and eventually the visualization frontend
need -- nothing outside this module should write raw SQL against these
four tables for read purposes, the same way queue/repository.py is the
one place that touches knowledge_injection_job.

Two of the four functions here (get_neighbors, find_path) use raw SQL via
a recursive CTE rather than the ORM -- same call made for claim_next_job
in queue/repository.py: a "walk the graph, tracking a visited-path, up to
N hops" query doesn't translate cleanly into chained ORM select()s, and
forcing it to would just be less readable SQL wearing a Python costume.

Scalability note: find_path's recursive CTE materializes every path up to
max_depth before picking the shortest, which is fine at the entity/
relation counts this schema has today but would need revisiting (e.g. a
real BFS with early termination, or Apache AGE) if the graph grows into
the tens of thousands of relations with high fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Attribute, Entity, Relation, Value


@dataclass(frozen=True, slots=True)
class EntityRef:
    """Minimal entity shape for graph nodes -- enough to render a node
    without pulling every Value row along with it."""

    id: UUID
    entity_type: str
    name: str
    label: str


@dataclass(frozen=True, slots=True)
class FactRef:
    """One resolved (namespace, attribute) -> value pair for an entity,
    with the attribute's own metadata alongside it."""

    namespace: str
    attribute_name: str
    value: str
    value_type: str
    multivalue: bool
    searchable: bool


@dataclass(frozen=True, slots=True)
class EntityDetail:
    """Everything a detail panel needs for one entity: itself plus every
    fact recorded about it."""

    entity: EntityRef
    facts: tuple[FactRef, ...]


@dataclass(frozen=True, slots=True)
class RelationRef:
    id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    relation_type: str


@dataclass(frozen=True, slots=True)
class GraphFragment:
    """A node/edge set shaped directly for the visualization layer (see
    the /graph/subgraph API response shape in the graph plan)."""

    nodes: tuple[EntityRef, ...]
    edges: tuple[RelationRef, ...]


def _to_entity_ref(entity: Entity) -> EntityRef:
    return EntityRef(id=entity.id, entity_type=entity.entity_type, name=entity.name, label=entity.label)


async def get_entity(session: AsyncSession, entity_id: UUID) -> EntityDetail | None:
    """One entity plus every Value recorded for it, each joined to its
    Attribute for namespace/name/value_type/multivalue metadata."""
    entity = await session.get(Entity, entity_id)
    if entity is None:
        return None

    rows = (
        await session.execute(
            select(Value, Attribute)
            .join(Attribute, Attribute.id == Value.attribute_id)
            .where(Value.entity_id == entity_id)
            .order_by(Attribute.namespace, Attribute.name)
        )
    ).all()

    facts = tuple(
        FactRef(
            namespace=attribute.namespace,
            attribute_name=attribute.name,
            value=value.value,
            value_type=attribute.value_type,
            multivalue=attribute.multivalue,
            searchable=value.searchable,
        )
        for value, attribute in rows
    )
    return EntityDetail(entity=_to_entity_ref(entity), facts=facts)


async def find_entities(
    session: AsyncSession,
    *,
    entity_type: str | None = None,
    name_query: str | None = None,
    limit: int = 50,
) -> tuple[EntityRef, ...]:
    """Search/filter entities -- backs a search box. `name_query` uses
    ILIKE for now; if fuzzy matching ever matters, add a pg_trgm GIN index
    on entity.name rather than changing this function's shape."""
    stmt = select(Entity)
    if entity_type is not None:
        stmt = stmt.where(Entity.entity_type == entity_type)
    if name_query:
        stmt = stmt.where(Entity.name.ilike(f"%{name_query}%"))
    stmt = stmt.order_by(Entity.name).limit(limit)

    entities = (await session.execute(stmt)).scalars().all()
    return tuple(_to_entity_ref(entity) for entity in entities)


# Relations aren't meaningfully directional for browsing -- a "founded by"
# edge should surface when looking at either endpoint. This CTE unions
# both directions of relation before walking outward, so `get_neighbors`
# and `find_path` traverse the graph as undirected even though the
# underlying FK (source_entity_id/target_entity_id) has a direction.
_NEIGHBORS_SQL = text(
    """
    WITH RECURSIVE undirected_relation AS (
        SELECT id, source_entity_id AS from_id, target_entity_id AS to_id, relation_type
        FROM relation
        UNION ALL
        SELECT id, target_entity_id AS from_id, source_entity_id AS to_id, relation_type
        FROM relation
    ),
    walk AS (
        SELECT
            CAST(:start_id AS uuid) AS entity_id,
            0 AS depth,
            ARRAY[CAST(:start_id AS uuid)] AS visited
        UNION ALL
        SELECT
            r.to_id,
            walk.depth + 1,
            walk.visited || r.to_id
        FROM walk
        JOIN undirected_relation r ON r.from_id = walk.entity_id
        WHERE walk.depth < :depth
          AND NOT (r.to_id = ANY(walk.visited))
    )
    SELECT DISTINCT entity_id FROM walk WHERE entity_id != CAST(:start_id AS uuid)
    """
)


async def get_neighbors(session: AsyncSession, entity_id: UUID, *, depth: int = 1) -> GraphFragment:
    """Every entity reachable from `entity_id` within `depth` hops
    (undirected), plus every relation connecting any two entities in that
    resulting set -- so the returned edges include relations *between*
    neighbors, not just ones touching the starting entity."""
    if depth < 1:
        raise ValueError("depth must be >= 1")

    neighbor_rows = (
        await session.execute(_NEIGHBORS_SQL, {"start_id": entity_id, "depth": depth})
    ).all()
    neighbor_ids = {row.entity_id for row in neighbor_rows}
    node_ids = neighbor_ids | {entity_id}

    entities = (
        await session.execute(select(Entity).where(Entity.id.in_(node_ids)))
    ).scalars().all()

    relations = (
        await session.execute(
            select(Relation).where(
                Relation.source_entity_id.in_(node_ids),
                Relation.target_entity_id.in_(node_ids),
            )
        )
    ).scalars().all()

    return GraphFragment(
        nodes=tuple(_to_entity_ref(entity) for entity in entities),
        edges=tuple(
            RelationRef(
                id=relation.id,
                source_entity_id=relation.source_entity_id,
                target_entity_id=relation.target_entity_id,
                relation_type=relation.relation_type,
            )
            for relation in relations
        ),
    )


_FIND_PATH_SQL = text(
    """
    WITH RECURSIVE undirected_relation AS (
        SELECT source_entity_id AS from_id, target_entity_id AS to_id
        FROM relation
        UNION ALL
        SELECT target_entity_id AS from_id, source_entity_id AS to_id
        FROM relation
    ),
    walk AS (
        SELECT
            CAST(:source_id AS uuid) AS entity_id,
            ARRAY[CAST(:source_id AS uuid)] AS path
        UNION ALL
        SELECT
            r.to_id,
            walk.path || r.to_id
        FROM walk
        JOIN undirected_relation r ON r.from_id = walk.entity_id
        WHERE array_length(walk.path, 1) < :max_depth
          AND NOT (r.to_id = ANY(walk.path))
    )
    SELECT path FROM walk
    WHERE entity_id = CAST(:target_id AS uuid)
    ORDER BY array_length(path, 1) ASC
    LIMIT 1
    """
)


async def find_path(
    session: AsyncSession, source_id: UUID, target_id: UUID, *, max_depth: int = 4
) -> tuple[EntityRef, ...] | None:
    """Shortest undirected path between two entities, up to `max_depth`
    hops, or None if unreachable within that bound. See the module
    docstring's scalability note -- this materializes every candidate
    path before picking the shortest, so keep max_depth modest."""
    if source_id == target_id:
        entity = await session.get(Entity, source_id)
        return (_to_entity_ref(entity),) if entity is not None else None

    row = (
        await session.execute(
            _FIND_PATH_SQL,
            {"source_id": source_id, "target_id": target_id, "max_depth": max_depth},
        )
    ).first()
    if row is None:
        return None

    entities_by_id = {
        entity.id: entity
        for entity in (
            await session.execute(select(Entity).where(Entity.id.in_(row.path)))
        )
        .scalars()
        .all()
    }
    # row.path is already in walk order (source -> ... -> target); preserve it.
    return tuple(_to_entity_ref(entities_by_id[entity_id]) for entity_id in row.path)

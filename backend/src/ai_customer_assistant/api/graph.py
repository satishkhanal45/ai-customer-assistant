"""
Read-only HTTP layer over ingestion/graph/queries.py. Every endpoint here
is a thin shape-conversion: call the query function, turn its dataclass
result into a Pydantic response model, return it. No query logic lives in
this file -- if you find yourself writing a WHERE clause here, it belongs
in queries.py instead.

All GET, all read-only -- no auth/mutation concerns to design around yet.

ASSUMPTION (unconfirmed -- I haven't seen your actual FastAPI app or how
it currently obtains a per-request AsyncSession): `get_session` below
builds its own engine/sessionmaker independently, the same stopgap
pattern used in scripts/run_worker.py and crawl_and_ingest.py. If your
app already has a shared session dependency (e.g. in api/deps.py or
similar), delete get_session here and import that one instead -- having
two independently-constructed engines in the same process is wasteful and
possibly wrong if they end up with different pool settings.
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.session import database_url
from ingestion.graph.queries import (
    EntityDetail,
    EntityRef,
    FactRef,
    GraphFragment,
    RelationRef,
    find_entities,
    find_path,
    get_entity,
    get_neighbors,
)

router = APIRouter(prefix="/graph", tags=["graph"])

def _async_database_url() -> str:
    sync_url = database_url()
    return sync_url.replace("postgresql+psycopg://", "postgresql+psycopg_async://")

# See module docstring's ASSUMPTION note -- swap this block for your app's
# real session dependency if one already exists.
_engine = create_async_engine(_async_database_url())
_session_factory = async_sessionmaker(bind=_engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Response models -- Pydantic mirrors of the dataclasses in queries.py.
# Kept as separate models (rather than returning the dataclasses directly)
# so the OpenAPI schema is explicit and stable regardless of internal
# dataclass changes.
# ---------------------------------------------------------------------------


class EntityRefResponse(BaseModel):
    id: UUID
    entity_type: str
    name: str
    label: str

    @classmethod
    def from_dataclass(cls, ref: EntityRef) -> "EntityRefResponse":
        return cls(id=ref.id, entity_type=ref.entity_type, name=ref.name, label=ref.label)


class FactRefResponse(BaseModel):
    namespace: str
    attribute_name: str
    value: str
    value_type: str
    multivalue: bool
    searchable: bool

    @classmethod
    def from_dataclass(cls, fact: FactRef) -> "FactRefResponse":
        return cls(
            namespace=fact.namespace,
            attribute_name=fact.attribute_name,
            value=fact.value,
            value_type=fact.value_type,
            multivalue=fact.multivalue,
            searchable=fact.searchable,
        )


class EntityDetailResponse(BaseModel):
    entity: EntityRefResponse
    facts: list[FactRefResponse]

    @classmethod
    def from_dataclass(cls, detail: EntityDetail) -> "EntityDetailResponse":
        return cls(
            entity=EntityRefResponse.from_dataclass(detail.entity),
            facts=[FactRefResponse.from_dataclass(fact) for fact in detail.facts],
        )


class RelationRefResponse(BaseModel):
    id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    relation_type: str

    @classmethod
    def from_dataclass(cls, relation: RelationRef) -> "RelationRefResponse":
        return cls(
            id=relation.id,
            source_entity_id=relation.source_entity_id,
            target_entity_id=relation.target_entity_id,
            relation_type=relation.relation_type,
        )


class GraphFragmentResponse(BaseModel):
    """Shaped directly for a graph-viz library: {nodes: [...], edges: [...]}."""

    nodes: list[EntityRefResponse]
    edges: list[RelationRefResponse]

    @classmethod
    def from_dataclass(cls, fragment: GraphFragment) -> "GraphFragmentResponse":
        return cls(
            nodes=[EntityRefResponse.from_dataclass(node) for node in fragment.nodes],
            edges=[RelationRefResponse.from_dataclass(edge) for edge in fragment.edges],
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/entities/{entity_id}", response_model=EntityDetailResponse)
async def read_entity(
    entity_id: UUID, session: AsyncSession = Depends(get_session)
) -> EntityDetailResponse:
    detail = await get_entity(session, entity_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"entity {entity_id} not found")
    return EntityDetailResponse.from_dataclass(detail)


@router.get("/search", response_model=list[EntityRefResponse])
async def search_entities(
    entity_type: str | None = Query(default=None),
    q: str | None = Query(default=None, description="fuzzy match against entity.name"),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[EntityRefResponse]:
    entities = await find_entities(session, entity_type=entity_type, name_query=q, limit=limit)
    return [EntityRefResponse.from_dataclass(entity) for entity in entities]


@router.get("/entities/{entity_id}/neighbors", response_model=GraphFragmentResponse)
async def read_neighbors(
    entity_id: UUID,
    depth: int = Query(default=1, ge=1, le=5),
    session: AsyncSession = Depends(get_session),
) -> GraphFragmentResponse:
    fragment = await get_neighbors(session, entity_id, depth=depth)
    return GraphFragmentResponse.from_dataclass(fragment)


# Alias matching the /graph/subgraph?entity_id=&depth= shape from the
# original plan -- same handler, different query-param-driven path instead
# of a path parameter, for callers that prefer everything as query params.
@router.get("/subgraph", response_model=GraphFragmentResponse)
async def read_subgraph(
    entity_id: UUID,
    depth: int = Query(default=1, ge=1, le=5),
    session: AsyncSession = Depends(get_session),
) -> GraphFragmentResponse:
    fragment = await get_neighbors(session, entity_id, depth=depth)
    return GraphFragmentResponse.from_dataclass(fragment)


@router.get("/path", response_model=list[EntityRefResponse] | None)
async def read_path(
    source: UUID,
    target: UUID,
    max_depth: int = Query(default=4, ge=1, le=8),
    session: AsyncSession = Depends(get_session),
) -> list[EntityRefResponse] | None:
    path = await find_path(session, source, target, max_depth=max_depth)
    if path is None:
        return None
    return [EntityRefResponse.from_dataclass(entity) for entity in path]

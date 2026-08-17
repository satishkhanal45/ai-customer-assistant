import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agents.knowledge.structured_lookup import (
    attribute_table,
    entity_table,
    metadata,
    relation_table,
    structured_lookup,
    value_table,
)
from agents.knowledge.types import StructuredQuery


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def populated(session):
    now = datetime.now(timezone.utc)
    company = _uid()
    flores_person = _uid()
    flores_person_canonical = _uid()
    employs = _uid()
    advisor = _uid()
    role = _uid()

    await session.execute(
        entity_table.insert(),
        [
            {"id": company, "label": company, "entity_type": "Company", "name": "Alpinist Studios", "created_at": now},
            {"id": flores_person, "label": flores_person, "entity_type": "person", "name": "Justin Flores", "created_at": now},
            {"id": flores_person_canonical, "label": flores_person_canonical, "entity_type": "Person", "name": "Justin Flores", "created_at": now},
        ],
    )
    await session.execute(
        attribute_table.insert(),
        [
            {"id": employs, "namespace": "person", "name": "role", "value_type": "string", "multivalue": False},
            {"id": advisor, "namespace": "Person", "name": "role", "value_type": "string", "multivalue": False},
            {"id": role, "namespace": "Company", "name": "employee", "value_type": "string", "multivalue": False},
        ],
    )
    # Relations split across the two entity-type variants.
    await session.execute(
        relation_table.insert(),
        [
            {"id": _uid(), "source_entity_id": company, "target_entity_id": flores_person, "relation_type": "employs", "created_at": now},
            {"id": _uid(), "source_entity_id": company, "target_entity_id": flores_person_canonical, "relation_type": "advisor", "created_at": now},
        ],
    )
    # A role value only exists under the canonical variant's namespace.
    await session.execute(
        value_table.insert(),
        [
            {"id": _uid(), "entity_id": flores_person_canonical, "attribute_id": advisor, "value": "Advisor", "searchable": True, "created_at": now},
        ],
    )
    await session.commit()
    return company, flores_person, flores_person_canonical


async def test_general_lookup_merges_type_variant_duplicates(session, populated):
    query = StructuredQuery(
        entity_type="Employee",
        entity_label="Justin Flores",
        attribute=None,
        relation_type=None,
        filters=(),
        confidence=0.9,
    )
    facts = await structured_lookup(query, session=session)
    relations = {fact.relation_type for fact in facts if fact.relation_type}
    assert relations == {"employs", "advisor"}
    # Incoming facts are oriented on the relation's source.
    assert all(fact.entity_label == "Alpinist Studios" for fact in facts if fact.relation_type)


async def test_attribute_lookup_falls_back_to_relations(session, populated):
    query = StructuredQuery(
        entity_type="Employee",
        entity_label="Justin Flores",
        attribute="email",
        relation_type=None,
        filters=(),
        confidence=0.9,
    )
    facts = await structured_lookup(query, session=session)
    relations = {fact.relation_type for fact in facts if fact.relation_type}
    assert relations == {"employs", "advisor"}


async def test_attribute_lookup_returns_value_when_present(session, populated):
    query = StructuredQuery(
        entity_type="Person",
        entity_label="Justin Flores",
        attribute="role",
        relation_type=None,
        filters=(),
        confidence=0.9,
    )
    facts = await structured_lookup(query, session=session)
    assert len(facts) == 1
    assert facts[0].value == "Advisor"
    assert facts[0].attribute == "role"

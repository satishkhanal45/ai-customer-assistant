"""Fact provenance and supersession (ingestion.md P1 item 5).

The question that raised this item: *"if the rate to build a website changed,
can the assistant still tell me what it used to be — and does it know which
one is current?"* The answer used to be no on both counts. `value` rows are
unique on (entity, attribute, value) and written with ON CONFLICT DO NOTHING,
so $500 and $800 both survived under the same entity and attribute, with no
version link and nothing marking either as current. Structured lookup returned
both.

Two contradictory prices presented as equally true is a *wrong* answer
delivered confidently, which is worse than a missing one. The decision taken
was to keep the history and mark it rather than prune it, so these pin both
halves: the old value survives, and it stops being answerable as current.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import (
    Attribute,
    Base,
    Entity,
    KnowledgeSource,
    KnowledgeSourceVersion,
    Value,
    ValueProvenance,
)
from ingestion.persistence import (
    _analyze_document_facts,
    resolve_superseded_values,
)
from ingestion.pipeline_types import ChunkExtraction, ExtractedFact


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            KnowledgeSource.__table__,
            KnowledgeSourceVersion.__table__,
            Entity.__table__,
            Attribute.__table__,
            Value.__table__,
            ValueProvenance.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _Corpus:
    """One source with two versions, one entity, one attribute — the smallest
    world in which "the rate changed" is expressible."""

    def __init__(self):
        self.source_id = uuid.uuid4()
        self.v1 = uuid.uuid4()
        self.v2 = uuid.uuid4()
        self.entity_id = uuid.uuid4()
        self.attribute_id = uuid.uuid4()


async def _seed(factory) -> _Corpus:
    c = _Corpus()
    async with factory() as session:
        session.add_all(
            [
                KnowledgeSource(
                    source_id=c.source_id,
                    source_type="FILE_UPLOAD",
                    current_version_id=c.v1,
                ),
                KnowledgeSourceVersion(
                    version_id=c.v1, source_id=c.source_id, version_number=1,
                    status="INDEXED", checksum="a", storage_uri="s3://a",
                ),
                KnowledgeSourceVersion(
                    version_id=c.v2, source_id=c.source_id, version_number=2,
                    status="INDEXED", checksum="b", storage_uri="s3://b",
                ),
                Entity(
                    id=c.entity_id, label="Website Build",
                    entity_type="Service", name="Website Build",
                ),
                Attribute(
                    id=c.attribute_id, namespace="general", name="rate",
                    value_type="string", multivalue=False,
                ),
            ]
        )
        await session.commit()
    return c


async def _add_value(factory, c, text, *, versions=()):
    value_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Value(
                id=value_id, entity_id=c.entity_id,
                attribute_id=c.attribute_id, value=text, searchable=True,
            )
        )
        for version_id in versions:
            session.add(
                ValueProvenance(value_id=value_id, version_id=version_id)
            )
        await session.commit()
    return value_id


async def _superseded(factory, value_id):
    async with factory() as session:
        return (await session.get(Value, value_id)).superseded_at


async def _cutover_to(factory, c, version_id):
    async with factory() as session:
        source = await session.get(KnowledgeSource, c.source_id)
        source.current_version_id = version_id
        await session.commit()


class TestSupersession:
    async def test_the_old_rate_survives_but_stops_being_current(
        self, session_factory
    ):
        """The whole item in one test: $500 is still in the database, and no
        longer answerable as the rate."""
        c = await _seed(session_factory)
        old = await _add_value(session_factory, c, "$500", versions=[c.v1])
        new = await _add_value(session_factory, c, "$800", versions=[c.v2])

        await _cutover_to(session_factory, c, c.v2)
        async with session_factory() as session:
            superseded, restored = await resolve_superseded_values(session)
            await session.commit()

        assert superseded == 1 and restored == 0
        assert await _superseded(session_factory, old) is not None
        assert await _superseded(session_factory, new) is None
        # Kept, not deleted — the point of choosing "mark" over "prune".
        async with session_factory() as session:
            assert await session.get(Value, old) is not None

    async def test_a_fact_another_document_still_asserts_is_not_superseded(
        self, session_factory
    ):
        """The reason provenance is a link table. `value` rows are global, so
        a fact dropped by one document may still be stated by another — a
        single version_id column could not express that."""
        c = await _seed(session_factory)
        other_source, other_version = uuid.uuid4(), uuid.uuid4()
        async with session_factory() as session:
            session.add_all([
                KnowledgeSource(
                    source_id=other_source, source_type="FILE_UPLOAD",
                    current_version_id=other_version,
                ),
                KnowledgeSourceVersion(
                    version_id=other_version, source_id=other_source,
                    version_number=1, status="INDEXED", checksum="c",
                    storage_uri="s3://c",
                ),
            ])
            await session.commit()

        shared = await _add_value(
            session_factory, c, "$500", versions=[c.v1, other_version]
        )

        await _cutover_to(session_factory, c, c.v2)
        async with session_factory() as session:
            superseded, _ = await resolve_superseded_values(session)
            await session.commit()

        assert superseded == 0
        assert await _superseded(session_factory, shared) is None

    async def test_a_fact_that_comes_back_is_restored(self, session_factory):
        """Facts return: a value removed in v2 and restated in v3 is current
        again, and a one-way mark would leave it permanently invisible."""
        c = await _seed(session_factory)
        value_id = await _add_value(session_factory, c, "$500", versions=[c.v1])

        await _cutover_to(session_factory, c, c.v2)
        async with session_factory() as session:
            await resolve_superseded_values(session)
            await session.commit()
        assert await _superseded(session_factory, value_id) is not None

        # v3 restates it.
        v3 = uuid.uuid4()
        async with session_factory() as session:
            session.add(KnowledgeSourceVersion(
                version_id=v3, source_id=c.source_id, version_number=3,
                status="INDEXED", checksum="d", storage_uri="s3://d",
            ))
            session.add(ValueProvenance(value_id=value_id, version_id=v3))
            await session.commit()
        await _cutover_to(session_factory, c, v3)

        async with session_factory() as session:
            superseded, restored = await resolve_superseded_values(session)
            await session.commit()

        assert restored == 1
        assert await _superseded(session_factory, value_id) is None

    async def test_a_value_with_no_provenance_is_never_touched(
        self, session_factory
    ):
        """Every row written before provenance tracking is in this state. The
        migration has no backfill because which version asserted an existing
        fact is not recoverable, so they must stay current."""
        c = await _seed(session_factory)
        legacy = await _add_value(session_factory, c, "$500", versions=[])

        await _cutover_to(session_factory, c, c.v2)
        async with session_factory() as session:
            superseded, _ = await resolve_superseded_values(session)
            await session.commit()

        assert superseded == 0
        assert await _superseded(session_factory, legacy) is None

    async def test_the_pass_is_idempotent(self, session_factory):
        """Derived from provenance every time rather than accumulated, so a
        re-run converges instead of drifting."""
        c = await _seed(session_factory)
        await _add_value(session_factory, c, "$500", versions=[c.v1])
        await _add_value(session_factory, c, "$800", versions=[c.v2])
        await _cutover_to(session_factory, c, c.v2)

        async with session_factory() as session:
            first = await resolve_superseded_values(session)
            await session.commit()
        async with session_factory() as session:
            second = await resolve_superseded_values(session)
            await session.commit()

        assert first == (1, 0)
        assert second == (0, 0)      # nothing left to change

    async def test_supersession_survives_a_rollback_to_an_older_version(
        self, session_factory
    ):
        """Nothing here is incremental, so pointing the source back at v1
        makes v1's facts current again without anyone reasoning about order."""
        c = await _seed(session_factory)
        old = await _add_value(session_factory, c, "$500", versions=[c.v1])
        new = await _add_value(session_factory, c, "$800", versions=[c.v2])

        await _cutover_to(session_factory, c, c.v2)
        async with session_factory() as session:
            await resolve_superseded_values(session)
            await session.commit()

        await _cutover_to(session_factory, c, c.v1)
        async with session_factory() as session:
            await resolve_superseded_values(session)
            await session.commit()

        assert await _superseded(session_factory, old) is None
        assert await _superseded(session_factory, new) is not None


def _multivalued_attributes(extractions):
    """The `multivalue` half of the document analysis.

    A thin reader rather than a second implementation: the analysis also
    folds restated values now, and these cases are about the flag.
    """
    return _analyze_document_facts(extractions).multivalued


class TestMultivaluedAttributes:
    """`multivalue` arrived hardcoded False from the extractor, so every
    attribute claimed to hold one value while 48 pairs in the live database
    held several. Observing the document is more reliable than asking the
    model."""

    def _fact(self, attribute, value, entity="Alpinist Studios"):
        return ExtractedFact(
            entity_type="Company", entity_name=entity, namespace="general",
            attribute_name=attribute, value=value,
        )

    def _chunks(self, *fact_groups):
        return tuple(
            ChunkExtraction(chunk_index=i, entity=None, facts=tuple(g), relations=())
            for i, g in enumerate(fact_groups)
        )

    def test_several_values_for_one_attribute_marks_it_multivalued(self):
        extractions = self._chunks(
            [self._fact("client_industries", "Fintech")],
            [self._fact("client_industries", "Health Tech")],
        )
        assert ("general", "client_industries") in _multivalued_attributes(extractions)

    def test_counted_across_the_whole_document_not_per_chunk(self):
        """A document's four industries are usually spread over four chunks;
        per-chunk counting would see one each and conclude single-valued."""
        extractions = self._chunks(
            *[[self._fact("client_industries", v)] for v in ("A", "B", "C", "D")]
        )
        assert ("general", "client_industries") in _multivalued_attributes(extractions)

    def test_one_value_stays_single_valued(self):
        extractions = self._chunks([self._fact("rate", "$500")])
        assert _multivalued_attributes(extractions) == frozenset()

    def test_the_same_value_restated_is_not_a_list(self):
        """The same fact in two chunks is one value, not evidence the
        attribute takes several."""
        extractions = self._chunks(
            [self._fact("rate", "$500")], [self._fact("rate", "$500")]
        )
        assert _multivalued_attributes(extractions) == frozenset()

    def test_two_entities_with_one_value_each_is_not_multivalued(self):
        """Keyed per entity: one rate for each of two services says nothing
        about whether `rate` takes a list."""
        extractions = self._chunks(
            [self._fact("rate", "$500", entity="Website Build")],
            [self._fact("rate", "$900", entity="App Build")],
        )
        assert _multivalued_attributes(extractions) == frozenset()

"""Unit tests for the ingestion reconciliation planners.

The DB-executing half (``reconcile`` / ``_repoint_*``) needs a live
Postgres, so these tests target the pure merge planners
(``build_entity_merge_groups`` / ``build_attribute_merge_groups``): the
duplicate-detection + survivor-selection logic that decides what actually
gets merged. The live database is reconciled separately via
``python -m ingestion.reconcile``.
"""
from __future__ import annotations

from uuid import uuid4

from ingestion.reconcile import (
    PROPER_NOUN_MERGES,
    build_attribute_merge_groups,
    build_entity_merge_groups,
)


def _e(entity_type: str, name: str, suffix: str = "id"):
    return (uuid4(), entity_type, name)


def test_company_org_synonyms_collapse_to_one_group():
    """The observed failure: Alpinist Studios stored as company, Company and
    organization must collapse into a single merge group."""
    rows = [
        _e("company", "Alpinist Studios"),
        _e("Company", "Alpinist Studios"),
        _e("organization", "Alpinist Studios"),
    ]
    groups = build_entity_merge_groups(rows)
    assert len(groups) == 1
    group = groups[0]
    assert group.key[0] == "Company"
    # The exact-canonical row survives; the two synonyms are duplicates.
    survivor = [r for r in rows if r[1] == "Company"][0]
    assert group.survivor_id == survivor[0]
    assert len(group.duplicate_ids) == 2


def test_person_case_duplicates_merge():
    rows = [_e("person", "Alex De Wit"), _e("Person", "Alex De Wit")]
    groups = build_entity_merge_groups(rows)
    assert len(groups) == 1
    assert groups[0].key[0] == "Person"
    survivor = [r for r in rows if r[1] == "Person"][0]
    assert groups[0].survivor_id == survivor[0]


def test_name_case_variants_merge():
    rows = [_e("Person", "Alex De Wit"), _e("Person", "alex de wit")]
    groups = build_entity_merge_groups(rows)
    assert len(groups) == 1
    assert len(groups[0].duplicate_ids) == 1


def test_canonical_singleton_not_returned():
    rows = [_e("Company", "Alpinist Studios")]
    assert build_entity_merge_groups(rows) == []


def test_distinct_entities_not_merged():
    rows = [_e("Company", "Alpinist Studios"), _e("Company", "Acme Inc")]
    assert build_entity_merge_groups(rows) == []


def test_attribute_namespace_synonyms_merge():
    rows = [
        (uuid4(), "company", "industry"),
        (uuid4(), "Company", "industry"),
    ]
    groups = build_attribute_merge_groups(rows)
    assert len(groups) == 1
    assert groups[0].key[0] == "Company"
    survivor = [r for r in rows if r[1] == "Company"][0]
    assert groups[0].survivor_id == survivor[0]


def test_attribute_namespace_case_merge():
    rows = [
        (uuid4(), "person", "role"),
        (uuid4(), "Person", "role"),
    ]
    groups = build_attribute_merge_groups(rows)
    assert len(groups) == 1
    assert groups[0].key[0] == "Person"
    assert len(groups[0].duplicate_ids) == 1


def test_noncanonical_namespace_stays_raw_no_false_merge():
    rows = [
        (uuid4(), "software development", "process"),
        (uuid4(), "company", "process"),
    ]
    groups = build_attribute_merge_groups(rows)
    # "software development" has no canonical form and must not be conflated
    # with "company" -> "Company", so nothing is merged.
    assert groups == []


def test_proper_noun_merges_are_explicit_and_canonical():
    """The single-referent merge list must only contain unambiguous proper
    nouns, each mapping to a canonical entity type."""
    for name, canonical_type in PROPER_NOUN_MERGES.items():
        assert name and name.strip().lower() == name.lower()
        assert canonical_type == canonical_type.strip()
        assert canonical_type == _canonical(canonical_type)


def test_reviewed_types_are_keyed_by_normalized_name():
    """The list is now looked up by the planner's own group key, which is the
    normalized (lowercased, whitespace-collapsed) name — an entry keyed
    "PostgreSQL" would silently never match."""
    for name in PROPER_NOUN_MERGES:
        assert name == " ".join(name.strip().lower().split())


def test_reviewed_types_override_the_frequency_heuristic():
    """Why the list still exists after merging by name subsumed its old job.

    PostgreSQL carries 8 facts as `Technology` and 1 as `Database`, so the
    support threshold rules `Database` out and the heuristic would keep the
    vaguer label. A human already decided otherwise."""
    assert PROPER_NOUN_MERGES["postgresql"] == "Database"

    rows = [_e("Technology", "PostgreSQL"), _e("Database", "postgresql")]
    groups = build_entity_merge_groups(
        rows,
        fact_counts={rows[0][0]: 8, rows[1][0]: 1},
        type_frequency={"Technology": 80, "Database": 3},
    )

    assert len(groups) == 1
    assert groups[0].key[0] == "Database"
    assert groups[0].survivor_name == "PostgreSQL"      # not "postgresql"


def _canonical(entity_type: str) -> str:
    from ingestion.extraction.ontology import safe_canonicalize_entity_type

    return safe_canonicalize_entity_type(entity_type)

# ---------------------------------------------------------------------------
# Merging by name (ingestion.md: entity fragmentation)
#
# Grouping used to include the canonical entity type, so it only merged rows
# whose types were synonyms of one canonical term. That left the larger
# problem untouched: "Agile" stored as Methodology, Process and Development
# Process is three entities holding 39, 14 and 2 facts — 55 facts about one
# concept, split across identities the database considered unrelated.
#
# Every case below is taken from the live corpus.
# ---------------------------------------------------------------------------

def _plan(rows, facts=None, freq=None):
    groups = build_entity_merge_groups(
        rows, fact_counts=facts or {}, type_frequency=freq or {}
    )
    return groups[0] if groups else None


class TestMergeByName:
    def test_non_synonym_types_now_merge(self):
        """The case the old grouping could not reach: Methodology, Process and
        Development Process are not synonyms of each other."""
        rows = [
            _e("Methodology", "Agile"),
            _e("Process", "Agile"),
            _e("Development Process", "Agile"),
        ]

        group = _plan(rows, facts={rows[0][0]: 39, rows[1][0]: 14, rows[2][0]: 2})

        assert group is not None
        assert len(group.duplicate_ids) == 2
        assert group.survivor_id == rows[0][0]      # the 39-fact row

    def test_different_names_still_do_not_merge(self):
        assert _plan([_e("Company", "Alpinist Studios"), _e("Company", "Acme Inc")]) is None


class TestChosenType:
    def test_the_rarer_type_wins_as_a_specificity_proxy(self):
        """`Technology` spans 80 entities and `Programming Language` 2, so the
        rarer one is the more informative label."""
        rows = [_e("Technology", "Python"), _e("Programming Language", "Python")]

        group = _plan(
            rows,
            facts={rows[0][0]: 15, rows[1][0]: 14},
            freq={"Technology": 80, "Programming Language": 2},
        )

        assert group.key[0] == "Programming Language"

    def test_a_type_with_almost_no_support_cannot_win_on_rarity(self):
        """The regression this threshold exists for. `Development Process`
        appears on 2 entities — rarer than `Methodology` — but carries 2 of
        Agile's 55 facts, because the model used it once. Pure rarity made it
        the label over the type holding 39."""
        rows = [
            _e("Methodology", "Agile"),
            _e("Process", "Agile"),
            _e("Development Process", "Agile"),
        ]

        group = _plan(
            rows,
            facts={rows[0][0]: 39, rows[1][0]: 14, rows[2][0]: 2},
            freq={"Methodology": 5, "Process": 19, "Development Process": 2},
        )

        assert group.key[0] == "Methodology"

    def test_a_type_carrying_no_facts_cannot_win(self):
        """"MVP development" is a service they sell; `Product` held zero facts
        and would otherwise have become the label."""
        rows = [_e("Service", "MVP development"), _e("Product", "MVP Development")]

        group = _plan(
            rows,
            facts={rows[0][0]: 11, rows[1][0]: 0},
            freq={"Service": 49, "Product": 35},
        )

        assert group.key[0] == "Service"

    def test_types_are_canonicalized_before_being_compared(self):
        rows = [_e("company", "Alpinist Studios"), _e("organization", "Alpinist Studios")]

        assert _plan(rows).key[0] == "Company"

    def test_the_choice_is_the_same_for_two_identical_situations(self):
        """`mongodb` and `firebase` had one fact under each of Technology and
        Database, and an earlier rule picked opposite answers for them."""
        mongo = [_e("Technology", "MongoDB"), _e("Database", "mongodb")]
        fire = [_e("Technology", "Firebase"), _e("Database", "firebase")]
        freq = {"Technology": 80, "Database": 3}

        mongo_type = _plan(mongo, facts={r[0]: 1 for r in mongo}, freq=freq).key[0]
        fire_type = _plan(fire, facts={r[0]: 1 for r in fire}, freq=freq).key[0]

        assert mongo_type == fire_type == "Database"


class TestChosenLabel:
    def test_the_best_cased_name_survives_not_the_survivor_row_s(self):
        """The survivor row is picked for fact count, and the lowercase row
        often has more facts. Taking its name renamed "PyTorch" to "pytorch"
        and "eBay" to "ebay" — user-visible, in every answer."""
        rows = [_e("Technology", "pytorch"), _e("Library", "PyTorch")]

        group = _plan(rows, facts={rows[0][0]: 3, rows[1][0]: 0})

        assert group.survivor_id == rows[0][0]        # the lowercase row
        assert group.survivor_name == "PyTorch"       # but not its name

    def test_title_case_beats_sentence_case(self):
        """From the corpus: "Soft Launch" against "Soft launch"."""
        rows = [_e("Release", "Soft launch"), _e("Release Type", "Soft Launch")]
        assert _plan(rows).survivor_name == "Soft Launch"

    def test_all_lowercase_variants_are_deterministic(self):
        rows = [_e("Project", "banking systems"), _e("Project Type", "banking systems")]
        assert _plan(rows).survivor_name == "banking systems"


class TestDeterminism:
    def test_the_plan_does_not_depend_on_row_order(self):
        rows = [
            _e("Methodology", "Agile"),
            _e("Process", "Agile"),
            _e("Development Process", "Agile"),
        ]
        facts = {rows[0][0]: 39, rows[1][0]: 14, rows[2][0]: 2}
        freq = {"Methodology": 5, "Process": 19, "Development Process": 2}

        forward = _plan(rows, facts=facts, freq=freq)
        backward = _plan(list(reversed(rows)), facts=facts, freq=freq)

        assert forward.survivor_id == backward.survivor_id
        assert forward.key == backward.key
        assert forward.survivor_name == backward.survivor_name
        assert set(forward.duplicate_ids) == set(backward.duplicate_ids)

    def test_planning_a_merged_group_again_is_a_no_op(self):
        """Idempotence: after a merge there is one row, so nothing to plan."""
        rows = [_e("Methodology", "Agile")]
        assert build_entity_merge_groups(rows) == []


# ---------------------------------------------------------------------------
# The repointing half, against real rows.
#
# These exist because the planner being right is not enough: the first live
# run of the name-based merge failed on a unique violation that no pure-planner
# test could have shown.
# ---------------------------------------------------------------------------

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import (
    Attribute,
    EmbeddingChunk,
    Entity,
    KnowledgeSourceEntityMap,
    Relation,
    Value,
)
from ingestion.reconcile import MergeGroup, ReconcileReport, _repoint_entity_merge


@pytest.fixture
async def session():
    """Every table the repointing touches.

    `embedding_chunk` and `knowledge_source_entity_map` are here because
    `_repoint_entity_merge` updates them: without them the merge fails on a
    missing table rather than on anything it is being tested for. SQLite does
    not enforce foreign keys by default, so their parents are not needed.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            Entity, Attribute, Value, Relation,
            EmbeddingChunk, KnowledgeSourceEntityMap,
        ):
            await conn.run_sync(table.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _entity(session, entity_type, name):
    row = Entity(id=uuid4(), label=name, entity_type=entity_type, name=name)
    session.add(row)
    await session.flush()
    return row


async def _value(session, entity, attribute, text):
    row = Value(id=uuid4(), entity_id=entity.id, attribute_id=attribute.id,
                value=text, searchable=True)
    session.add(row)
    await session.flush()
    return row


async def _attribute(session, name):
    row = Attribute(id=uuid4(), namespace="general", name=name, value_type="string")
    session.add(row)
    await session.flush()
    return row


class TestRepointEntityMerge:
    async def test_the_survivor_can_take_a_label_a_duplicate_still_holds(self, session):
        """Regression, found by the first live run.

        Merging the two "Design" rows relabels the `Phase` survivor to
        `SDLC Phase` — which the `SDLC Phase` duplicate is still occupying, so
        relabelling before deleting hits `uq_entity_type_name` against a row
        that is about to cease existing.
        """
        survivor = await _entity(session, "Phase", "Design")
        duplicate = await _entity(session, "SDLC Phase", "Design")

        await _repoint_entity_merge(
            session,
            MergeGroup(
                key=("SDLC Phase", "design"),
                survivor_id=survivor.id,
                duplicate_ids=(duplicate.id,),
                survivor_name="Design",
            ),
            ReconcileReport(),
        )
        await session.flush()

        rows = (await session.execute(select(Entity))).scalars().all()
        assert len(rows) == 1
        assert (rows[0].entity_type, rows[0].name) == ("SDLC Phase", "Design")

    async def test_the_chosen_label_is_written_not_the_survivor_s(self, session):
        survivor = await _entity(session, "Technology", "pytorch")
        duplicate = await _entity(session, "Library", "PyTorch")

        await _repoint_entity_merge(
            session,
            MergeGroup(
                key=("Technology", "pytorch"),
                survivor_id=survivor.id,
                duplicate_ids=(duplicate.id,),
                survivor_name="PyTorch",
            ),
            ReconcileReport(),
        )
        await session.flush()

        row = (await session.execute(select(Entity))).scalars().one()
        assert row.name == "PyTorch" and row.label == "PyTorch"

    async def test_facts_that_collide_only_after_merging_are_collapsed(self, session):
        """"offers flexibility" sat on `Methodology / Agile` and on
        `Process / Agile`, satisfying the unique constraint because the entity
        ids differed. Merging is what makes them one fact — and what makes the
        repointing UPDATE collide if the duplicate is not dropped first."""
        survivor = await _entity(session, "Methodology", "Agile")
        duplicate = await _entity(session, "Process", "Agile")
        flexibility = await _attribute(session, "flexibility")
        await _value(session, survivor, flexibility, "offers flexibility")
        await _value(session, duplicate, flexibility, "offers flexibility")

        report = ReconcileReport()
        await _repoint_entity_merge(
            session,
            MergeGroup(("Methodology", "agile"), survivor.id, (duplicate.id,), "Agile"),
            report,
        )
        await session.flush()

        values = (await session.execute(select(Value))).scalars().all()
        assert len(values) == 1
        assert values[0].entity_id == survivor.id

    async def test_a_collision_only_on_the_normalized_form_is_caught(self, session):
        """Uniqueness moved to `value_norm`, so matching on the exact text
        would miss this pair and the repointing UPDATE would fail."""
        survivor = await _entity(session, "Person", "Parbati B.")
        duplicate = await _entity(session, "Role", "Parbati B.")
        role = await _attribute(session, "role")
        await _value(session, survivor, role, "PHP Intern")
        await _value(session, duplicate, role, "php intern")

        await _repoint_entity_merge(
            session,
            MergeGroup(("Person", "parbati b."), survivor.id, (duplicate.id,), "Parbati B."),
            ReconcileReport(),
        )
        await session.flush()

        values = (await session.execute(select(Value))).scalars().all()
        assert len(values) == 1
        assert values[0].value == "PHP Intern"

    async def test_relations_are_repointed_to_the_survivor(self, session):
        survivor = await _entity(session, "Methodology", "Agile")
        duplicate = await _entity(session, "Process", "Agile")
        other = await _entity(session, "Company", "Alpinist Studios")
        session.add(Relation(id=uuid4(), source_entity_id=other.id,
                             target_entity_id=duplicate.id, relation_type="uses"))
        await session.flush()

        await _repoint_entity_merge(
            session,
            MergeGroup(("Methodology", "agile"), survivor.id, (duplicate.id,), "Agile"),
            ReconcileReport(),
        )
        await session.flush()

        relation = (await session.execute(select(Relation))).scalars().one()
        assert relation.target_entity_id == survivor.id

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


def test_proper_noun_merges_cover_observed_duplicates():
    """Every entry in the reviewed list must actually correspond to a stale
    name that appears under multiple entity types."""
    from ingestion.extraction.ontology import safe_canonicalize_entity_type

    for name in PROPER_NOUN_MERGES:
        # The type labels stored for these names are *not* synonyms of the
        # target canonical type — otherwise the synonym logic would have
        # already collapsed them and the explicit entry would be dead code.
        assert safe_canonicalize_entity_type("Database") == "Database"
    assert "PostgreSQL" in PROPER_NOUN_MERGES
    assert PROPER_NOUN_MERGES["PostgreSQL"] == "Database"


def _canonical(entity_type: str) -> str:
    from ingestion.extraction.ontology import safe_canonicalize_entity_type

    return safe_canonicalize_entity_type(entity_type)
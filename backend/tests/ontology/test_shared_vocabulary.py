"""The ingestion and retrieval ontologies must never drift apart (P2-1).

Both pipelines used to carry forked ~750-line copies of the vocabulary. They
drifted, and the failure mode was silent: ingestion would canonicalize a term
one way, retrieval another (or not at all), so facts were written that no
query could ever resolve. Nothing raised.

These tests fail the build if the two sides ever stop agreeing again. The
strongest of them assert *identity* — both modules must resolve to the same
table objects, which is only possible while a single shared vocabulary backs
both — because equality alone would pass again the moment someone re-forks
the tables and keeps them momentarily in sync.
"""
from __future__ import annotations

import pytest

from agents.knowledge import ontology as knowledge
from ingestion.extraction import ontology as ingestion
from ontology import vocabulary

SHARED_TABLES = [
    "ALL_ENTITY_TYPES",
    "ATTRIBUTE_SYNONYMS",
    "ATTRIBUTE_VALUE_TYPES",
    "DOMAIN_ENTITY_TYPES",
    "ENTITY_TYPE_ATTRIBUTES",
    "ENTITY_TYPE_SYNONYMS",
    "ENTITY_TYPE_TO_DOMAIN",
    "RELATION_TYPE_SYNONYMS",
    "RELATION_TYPE_VOCABULARY",
]


class TestNoDrift:
    @pytest.mark.parametrize("table", SHARED_TABLES)
    def test_both_pipelines_read_the_same_table_object(self, table):
        """Identity, not equality: re-forking the tables would fail here even
        if the copies started out identical."""
        assert getattr(knowledge, table) is getattr(vocabulary, table)
        assert getattr(ingestion, table) is getattr(vocabulary, table)

    def test_every_entity_type_ingestion_can_write_is_resolvable_by_retrieval(self):
        """Ingestion must not be able to store an entity type that a query
        can never canonicalize onto."""
        unresolvable = []
        for entity_type in vocabulary.ALL_ENTITY_TYPES:
            try:
                knowledge.canonicalize_entity_type(entity_type)
            except Exception:
                unresolvable.append(entity_type)
        assert not unresolvable, f"retrieval cannot resolve: {unresolvable}"

    def test_every_synonym_resolves_identically_on_both_sides(self):
        """The exact class of bug that shipped: 'mobile developer' resolved to
        `Person` during ingestion and to `Mobile App` during retrieval, at 0.88
        confidence — a confident wrong answer rather than a visible failure."""
        mismatches = {}
        for synonym, expected in vocabulary.ENTITY_TYPE_SYNONYMS.items():
            k = knowledge.canonicalize_entity_type(synonym).canonical_term
            i = ingestion.canonicalize_entity_type(synonym).canonical_term
            if not (k == i == expected):
                mismatches[synonym] = {"expected": expected, "knowledge": k, "ingestion": i}
        assert not mismatches, f"synonyms resolve differently: {mismatches}"

    def test_every_attribute_ingestion_can_write_is_resolvable_by_retrieval(self):
        """`Person.salary` was writable by ingestion and unresolvable by
        retrieval — a fact that could be stored but never asked for."""
        unresolvable = []
        for entity_type, attributes in vocabulary.ENTITY_TYPE_ATTRIBUTES.items():
            for attribute in attributes:
                try:
                    knowledge.canonicalize_attribute(entity_type, attribute)
                except Exception:
                    unresolvable.append(f"{entity_type}.{attribute}")
        assert not unresolvable, f"retrieval cannot resolve: {unresolvable}"

    def test_every_relation_synonym_resolves_identically(self):
        mismatches = {
            synonym: (k, i)
            for synonym, expected in vocabulary.RELATION_TYPE_SYNONYMS.items()
            if (k := knowledge.canonicalize_relation_type(synonym).canonical_term)
            != (i := ingestion.canonicalize_relation_type(synonym).canonical_term)
        }
        assert not mismatches, f"relations resolve differently: {mismatches}"


class TestRegressionsThatShipped:
    """The three concrete divergences found in the forked copies."""

    @pytest.mark.parametrize(
        "wording, expected",
        [("data engineer", "Person"), ("mobile developer", "Person")],
    )
    def test_role_wording_resolves_to_person_on_both_sides(self, wording, expected):
        assert knowledge.canonicalize_entity_type(wording).canonical_term == expected
        assert ingestion.canonicalize_entity_type(wording).canonical_term == expected

    def test_person_salary_is_resolvable_by_retrieval(self):
        assert knowledge.canonicalize_attribute("Person", "salary").canonical_term == "salary"
        assert knowledge.value_type_for_attribute("salary") == "number"


class TestPackagePoliciesArePreserved:
    """Sharing the vocabulary must not have flattened the two packages'
    deliberately different error semantics."""

    def test_knowledge_raises_its_own_exception_types(self):
        from agents.knowledge.exceptions import UnknownAttributeError, UnknownEntityTypeError

        with pytest.raises(UnknownEntityTypeError):
            knowledge.canonicalize_entity_type("qqq zzz nonsense")
        with pytest.raises(UnknownAttributeError):
            knowledge.canonicalize_attribute("Company", "qqq zzz nonsense")

    def test_ingestion_raises_its_own_exception_types(self):
        with pytest.raises(ingestion.UnknownEntityTypeError):
            ingestion.canonicalize_entity_type("qqq zzz nonsense")
        with pytest.raises(ingestion.UnknownAttributeError):
            ingestion.canonicalize_attribute("Company", "qqq zzz nonsense")

    def test_ingestion_safe_variants_never_raise(self):
        """One odd string from the model must not abort a whole document."""
        assert ingestion.safe_canonicalize_entity_type("qqq zzz nonsense") == "qqq zzz nonsense"
        assert ingestion.safe_canonicalize_attribute("Company", "qqq zzz") == "qqq zzz"
        assert ingestion.safe_canonicalize_entity_type(None) == ""
        assert ingestion.safe_canonicalize_entity_type("  ") == "  "

    def test_ingestion_still_renders_the_prompt_reference(self):
        reference = ingestion.formatted_ontology_reference()
        assert "Organization:" in reference
        assert "Company" in reference
        assert len(reference.splitlines()) == len(vocabulary.DOMAIN_ENTITY_TYPES)

    @pytest.mark.parametrize(
        "wording, retrieval_guess",
        [("customer", "Cluster"), ("plan tier", "Partner")],
    )
    def test_ingestion_refuses_to_fuzzy_guess_an_entity_type(self, wording, retrieval_guess):
        """Sharing the vocabulary must NOT flatten the fuzzy/exact asymmetry.

        Ingestion writes to the database, where a wrong canonical type merges
        two different real-world entities forever, so it resolves exact +
        synonym only and otherwise keeps the raw string. Retrieval only reads,
        so it may guess. `"customer"` fuzzy-matching to `"Cluster"` is the
        documented example — harmless as a failed query, corrupting as a write.
        """
        assert ingestion.safe_canonicalize_entity_type(wording) == wording
        with pytest.raises(ingestion.UnknownEntityTypeError):
            ingestion.canonicalize_entity_type(wording)

        # Retrieval still guesses — that is the intended asymmetry, not a bug.
        assert knowledge.canonicalize_entity_type(wording).canonical_term == retrieval_guess

    def test_ingestion_refuses_to_fuzzy_guess_an_attribute(self):
        with pytest.raises(ingestion.UnknownAttributeError):
            ingestion.canonicalize_attribute("Company", "industri")
        assert knowledge.canonicalize_attribute("Company", "industri").canonical_term == "industry"

    def test_relation_types_fuzzy_match_on_both_sides(self):
        """relation_type has no CHECK constraint, so a wrong guess there is
        recoverable free text rather than a corrupted identity — both sides
        may fuzzy-match."""
        assert knowledge.canonicalize_relation_type("depend on").confidence < 1.0
        assert ingestion.canonicalize_relation_type("depend on").canonical_term == (
            knowledge.canonicalize_relation_type("depend on").canonical_term
        )

    def test_relation_miss_degrades_instead_of_raising(self):
        """relation_type has no CHECK constraint, so free text is schema-valid."""
        result = knowledge.canonicalize_relation_type("zzz vvv")
        assert result is not None
        assert result.canonical_term == "zzz_vvv"
        assert result.confidence < 0.5
        assert knowledge.canonicalize_relation_type(None) is None
        assert knowledge.canonicalize_relation_type("   ") is None


class TestVocabularyIntegrity:
    def test_every_referenced_attribute_has_a_value_type(self):
        """Mirrors the module-load assertion: a missing value_type would
        violate the attribute.value_type CHECK constraint on first write."""
        referenced = {
            attribute
            for attributes in vocabulary.ENTITY_TYPE_ATTRIBUTES.values()
            for attribute in attributes
        }
        assert not referenced - set(vocabulary.ATTRIBUTE_VALUE_TYPES)

    def test_value_types_satisfy_the_check_constraint(self):
        allowed = {"string", "number", "boolean", "date", "json"}
        assert set(vocabulary.ATTRIBUTE_VALUE_TYPES.values()) <= allowed

    def test_every_entity_type_has_an_attribute_set(self):
        assert set(vocabulary.ALL_ENTITY_TYPES) == set(vocabulary.ENTITY_TYPE_ATTRIBUTES)

    def test_every_synonym_targets_a_real_entity_type(self):
        unknown = set(vocabulary.ENTITY_TYPE_SYNONYMS.values()) - set(vocabulary.ALL_ENTITY_TYPES)
        assert not unknown, f"synonyms point at non-existent entity types: {sorted(unknown)}"

    def test_every_attribute_synonym_targets_a_real_attribute(self):
        unknown = set(vocabulary.ATTRIBUTE_SYNONYMS.values()) - set(vocabulary.ATTRIBUTE_VALUE_TYPES)
        assert not unknown, f"attribute synonyms point at unknown attributes: {sorted(unknown)}"

    def test_every_relation_synonym_targets_a_real_relation(self):
        unknown = set(vocabulary.RELATION_TYPE_SYNONYMS.values()) - set(
            vocabulary.RELATION_TYPE_VOCABULARY
        )
        assert not unknown, f"relation synonyms point at unknown relations: {sorted(unknown)}"

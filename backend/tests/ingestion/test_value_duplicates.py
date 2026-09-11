"""Folding restatements of one fact into one fact.

The corpus held five `MVP / definition` values where the document states at
most two definitions, and `Parbati B. / role` as both "PHP Intern" and
"php intern". Structured lookup returned all of them, as though the documents
had said that many different things.

The hard constraint, and the reason these rules are conservative: plenty of
attributes genuinely take several values. `Agile / stage` holds six real
stages and `Alpinist Studios / objective` six real objectives. Collapsing
those would turn a correct answer into a lossy one, which is worse than the
duplicates. Every case below is taken from the live database.
"""
from __future__ import annotations

from ingestion.persistence import _analyze_document_facts
from ingestion.pipeline_types import ChunkExtraction, ExtractedFact
from ingestion.values import (
    NEAR_DUPLICATE_RATIO,
    collapse_near_duplicates,
    normalize_value,
)


class TestNormalizeValue:
    def test_case_is_not_a_difference(self):
        assert normalize_value("PHP Intern") == normalize_value("php intern")

    def test_a_unicode_hyphen_is_not_a_difference(self):
        """U+2011 NON-BREAKING HYPHEN against U+002D. NFKC alone folds it only
        as far as U+2010, which is still not the ASCII the rest of the corpus
        uses, so the dash class is mapped explicitly."""
        assert normalize_value("pre-defined projects") == normalize_value(
            "pre‑defined projects"
        )

    def test_curly_quotes_are_not_a_difference(self):
        assert normalize_value("the client’s brief") == normalize_value(
            "the client's brief"
        )

    def test_whitespace_runs_collapse(self):
        assert normalize_value("  offers   flexibility\n") == "offers flexibility"

    def test_numbers_keep_their_separators(self):
        """Deliberate. A rule loose enough to merge "4,000 - 7,000" with
        "4000-7000" is loose enough to merge two genuinely different prices,
        and this corpus has a pair of those."""
        assert normalize_value("4,000 - 7,000") != normalize_value("4000-7000")

    def test_the_normalized_form_is_never_what_is_displayed(self):
        """It is a comparison key. The original text is the answer."""
        assert normalize_value("PHP Intern") == "php intern"


class TestCollapseNearDuplicates:
    def test_one_definition_told_twice_becomes_one(self):
        """Differ only by "while it is not" against "while not"."""
        long_form = (
            "functional version of the product that includes all the basic "
            "features required to solve the problem while it is not polished "
            "with final design and features"
        )
        short_form = long_form.replace("while it is not", "while not")

        assert collapse_near_duplicates([long_form, short_form]) == [long_form]

    def test_the_longer_telling_survives(self):
        """The shorter is usually the less specific version of the same fact,
        and the longer carries strictly more of the answer."""
        assert collapse_near_duplicates(
            [
                "validate design or concept before full development",
                "validate design or concept before full development phase",
            ]
        ) == ["validate design or concept before full development phase"]

    def test_a_genuinely_multivalued_attribute_is_untouched(self):
        """The case that must never break: six real stages of Agile."""
        stages = ["planning", "design", "development", "testing", "release", "feedback"]
        assert collapse_near_duplicates(stages) == stages

    def test_six_real_objectives_are_untouched(self):
        objectives = [
            "build long-term relationships founded on trust, transparency, and accountability.",
            "deliver software that is secure, reliable, maintainable, and scalable.",
            "enable organizations to adopt modern technologies without unnecessary complexity.",
            "encourage continuous improvement in engineering practices and delivery processes.",
            "invest in employee development to ensure technical excellence across all disciplines.",
            "promote ethical and responsible use of emerging technologies, particularly artificial intelligence.",
        ]
        assert collapse_near_duplicates(objectives) == objectives

    def test_two_different_prices_are_never_merged(self):
        """0.706 similar, and merging them would delete an answer."""
        assert collapse_near_duplicates(["0-8000", "4,000 - 8,000"]) == [
            "0-8000",
            "4,000 - 8,000",
        ]

    def test_two_different_answers_are_never_merged(self):
        assert collapse_near_duplicates(
            ["Mobile App Development", "Web Development"]
        ) == ["Mobile App Development", "Web Development"]

    def test_two_different_urls_are_never_merged(self):
        """0.893 similar — just under the line, and on the right side of it."""
        assert len(
            collapse_near_duplicates(
                ["https://alpiniststudios.com/", "https://alpiniststudios.com/about"]
            )
        ) == 2

    def test_case_variants_collapse(self):
        assert collapse_near_duplicates(["PHP Intern", "php intern"]) == ["PHP Intern"]

    def test_input_order_is_preserved(self):
        values = ["testing", "planning", "design"]
        assert collapse_near_duplicates(values) == values

    def test_a_single_value_is_returned_unchanged(self):
        assert collapse_near_duplicates(["only one"]) == ["only one"]
        assert collapse_near_duplicates([]) == []

    def test_the_threshold_sits_above_the_ambiguous_band(self):
        """Documented because it was measured, not chosen: real duplicates and
        real distinctions overlap between 0.70 and 0.91, so the line goes
        above that band. Missing a duplicate leaves a redundant row; merging
        two prices deletes an answer."""
        assert NEAR_DUPLICATE_RATIO > 0.91


class TestDocumentAnalysis:
    def _facts(self, *pairs, entity="MVP"):
        return tuple(
            ChunkExtraction(
                chunk_index=i,
                entity=None,
                facts=(
                    ExtractedFact(
                        entity_type="Concept",
                        entity_name=entity,
                        namespace="general",
                        attribute_name=attribute,
                        value=value,
                    ),
                ),
                relations=(),
            )
            for i, (attribute, value) in enumerate(pairs)
        )

    def test_restatements_across_chunks_are_folded(self):
        """The usual shape: two overlapping windows, two tellings, two chunks."""
        long_form = (
            "functional version of the product that includes all the basic "
            "features required to solve the problem while it is not polished"
        )
        extractions = self._facts(
            ("definition", long_form),
            ("definition", long_form.replace("while it is not", "while not")),
        )

        document = _analyze_document_facts(extractions)

        key = ("Concept", "MVP", "general", "definition")
        assert len(document.surviving[key]) == 1

    def test_a_folded_attribute_is_not_reported_as_multivalued(self):
        """Order matters. Deriving `multivalue` before collapsing would read
        two tellings of one definition as evidence that `definition` takes
        several values — the opposite of what they are evidence of."""
        long_form = "a" * 60 + " told one way"
        extractions = self._facts(
            ("definition", long_form),
            ("definition", long_form.replace("one way", "one way!")),
        )

        assert _analyze_document_facts(extractions).multivalued == frozenset()

    def test_a_genuinely_multivalued_attribute_still_is(self):
        extractions = self._facts(
            ("stage", "planning"), ("stage", "design"), ("stage", "release")
        )

        document = _analyze_document_facts(extractions)

        assert ("general", "stage") in document.multivalued
        assert len(document.surviving[("Concept", "MVP", "general", "stage")]) == 3

    def test_values_for_different_entities_are_kept_apart(self):
        """One rate each for two services says nothing about either."""
        extractions = self._facts(("rate", "$500"), entity="Website Build") + self._facts(
            ("rate", "$900"), entity="App Build"
        )

        document = _analyze_document_facts(extractions)

        assert document.multivalued == frozenset()
        assert document.surviving[("Concept", "Website Build", "general", "rate")] == {
            "$500"
        }

    def test_keeps_defaults_to_true_for_an_unseen_key(self):
        """A fact the analysis never saw must be written, not silently
        dropped — the analysis is an optimisation, not an allowlist."""
        document = _analyze_document_facts(())
        assert document.keeps(("Concept", "MVP", "general", "definition"), "anything")

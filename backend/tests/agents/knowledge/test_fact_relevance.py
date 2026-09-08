"""An unfiltered entity dump must not crowd the answer out of the prompt (F4).

`structured_lookup`'s `general` shape — no attribute, no relation requested —
returns everything the graph knows about the entity. That dump went into the
prompt under "Structured Facts", ahead of the documentation, with the answer
prompt instructing the model to *prefer* it. Measured live on "What are the
core values of the company?", the same question three times:

    facts=0   chunks=4  ->  grounded, correct answer
    facts=15  chunks=4  ->  NOT grounded, "I don't have information..."
    facts=0   chunks=4  ->  grounded, correct answer

All 15 facts were about Agile delivery ceremonies. The documentation right
below them contained "core value" and "integrity". **The run that retrieved
more was the run that failed** — an authoritative-looking section that answers
nothing reads as evidence of absence.
"""
from __future__ import annotations

import pytest
from conftest import fake_session_factory

from agents.knowledge.fact_relevance import (
    fact_terms,
    is_relevant,
    is_slotted,
    relevant_facts,
)
from agents.knowledge.types import StructuredFact, StructuredQuery


def _attribute_fact(attribute: str, value: str, entity: str = "Company") -> StructuredFact:
    return StructuredFact(
        entity_id="e1", entity_type=entity, entity_label=entity,
        attribute=attribute, value=value, value_type="string", confidence=1.0,
    )


def _relation_fact(relation: str, related: str, entity: str = "Company") -> StructuredFact:
    return StructuredFact(
        entity_id="e1", entity_type=entity, entity_label=entity,
        attribute="", value="", value_type="string",
        relation_type=relation, related_entity_label=related, confidence=1.0,
    )


# The dump that actually shipped, verbatim from the live run.
THE_DUMP = (
    _attribute_fact("delivery_methodology", "Agile Delivery Practices"),
    _relation_fact("uses", "Agile Delivery Practices"),
    _relation_fact("uses", "Sprint Planning"),
    _relation_fact("uses", "Daily Collaboration"),
    _relation_fact("uses", "Development"),
    _relation_fact("uses", "Sprint Review"),
    _relation_fact("uses", "Retrospective"),
    _relation_fact("uses", "Weekly Status Report"),
    _relation_fact("uses", "Sprint Demo"),
    _relation_fact("uses", "Risk Tracking"),
    _relation_fact("uses", "Decision Log"),
    _relation_fact("uses", "Shared Documentation"),
    _relation_fact("uses", "Architecture Review"),
    _relation_fact("uses", "Escalation Procedure"),
    _relation_fact("serves", "Clients"),
)

UNSLOTTED = StructuredQuery(
    entity_type="Company", entity_label="company", attribute=None,
    relation_type=None, filters=(), confidence=0.62,
)


class TestTheRegressionThatShipped:
    def test_the_agile_dump_is_dropped_for_a_question_about_values(self):
        kept = relevant_facts(
            THE_DUMP, query=UNSLOTTED,
            query_text="What are the core values of the company?",
        )
        assert kept == (), f"kept {len(kept)} irrelevant facts: {[f.value for f in kept]}"

    def test_the_same_dump_survives_for_a_question_it_answers(self):
        """The filter must not simply delete general lookups — when the dump
        is on topic it is exactly the anchor hybrid retrieval exists for."""
        kept = relevant_facts(
            THE_DUMP, query=UNSLOTTED,
            query_text="What delivery methodology does the company use?",
        )
        assert kept, "a question about delivery methodology kept no delivery facts"
        assert any("Agile" in f.value or f.related_entity_label == "Agile Delivery Practices"
                   for f in kept)

    def test_a_question_about_sprints_keeps_the_sprint_facts(self):
        kept = relevant_facts(
            THE_DUMP, query=UNSLOTTED, query_text="Do you run sprint reviews?"
        )
        labels = {f.related_entity_label for f in kept}
        assert "Sprint Review" in labels
        assert "Escalation Procedure" not in labels


class TestSlottedLookupsPassThrough:
    def test_an_attribute_request_is_never_filtered(self):
        """The customer asked for this exact slot; whatever came back is the
        answer to the question, however oddly it is worded."""
        query = StructuredQuery(
            entity_type="Company", entity_label="Alpinist Studios",
            attribute="industry", relation_type=None, filters=(), confidence=0.9,
        )
        facts = (_attribute_fact("industry", "software consulting"),)
        assert relevant_facts(facts, query=query, query_text="zzz vvv") == facts

    def test_a_relation_request_is_never_filtered(self):
        query = StructuredQuery(
            entity_type="Company", entity_label="Alpinist Studios", attribute=None,
            relation_type="serves", filters=(), confidence=0.9,
        )
        facts = (_relation_fact("serves", "Clients"),)
        assert relevant_facts(facts, query=query, query_text="zzz vvv") == facts

    def test_is_slotted_distinguishes_the_three_lookup_shapes(self):
        assert is_slotted(UNSLOTTED) is False
        assert is_slotted(None) is False
        assert is_slotted(
            StructuredQuery(entity_type="C", entity_label=None, attribute="industry",
                            relation_type=None, filters=(), confidence=0.9)
        ) is True
        assert is_slotted(
            StructuredQuery(entity_type="C", entity_label=None, attribute=None,
                            relation_type="uses", filters=(), confidence=0.9)
        ) is True


class TestItDegradesRatherThanDeletes:
    def test_no_query_text_keeps_everything(self):
        """Discarding facts because the filter had nothing to work with would
        be a worse failure than the one it prevents."""
        assert relevant_facts(THE_DUMP, query=UNSLOTTED, query_text=None) == THE_DUMP
        assert relevant_facts(THE_DUMP, query=UNSLOTTED, query_text="") == THE_DUMP

    def test_a_question_made_only_of_stopwords_keeps_everything(self):
        assert relevant_facts(THE_DUMP, query=UNSLOTTED, query_text="what about the company?") == THE_DUMP

    def test_no_facts_is_not_an_error(self):
        assert relevant_facts((), query=UNSLOTTED, query_text="anything") == ()


class TestTheMatchingRule:
    def test_the_entity_is_excluded_from_a_fact_terms(self):
        """Including the entity label would make every fact about it match any
        question naming it — which is exactly the case that broke."""
        terms = fact_terms(_relation_fact("uses", "Sprint Planning", entity="Alpinist"))
        assert "alpinist" not in terms
        assert {"uses", "sprint", "planning"} <= terms

    def test_ordinary_inflection_still_matches(self):
        fact = _relation_fact("uses", "Kubernetes orchestration")
        assert is_relevant(fact, {"use"}) is True
        assert is_relevant(fact, {"orchestrate"}) is True

    def test_a_short_coincidental_prefix_does_not_match(self):
        """"use" vs "user" is fine; "dev" vs "delivery" is not — a 3-letter
        prefix would match far too much."""
        fact = _relation_fact("uses", "Development")
        assert is_relevant(fact, {"dev"}) is False

    def test_generic_terms_do_not_make_everything_relevant(self):
        for noise in ("what", "the", "tell", "company"):
            assert is_relevant(_relation_fact("uses", "Sprint Planning"), {noise}) is False


class TestTheWholeRetrievalPathAppliesIt:
    """The filter is worthless if the production path skips it — so it is
    asserted on the node the graph runs and on the compiled graph itself."""

    async def test_the_structured_lookup_node_filters_the_dump(self, monkeypatch):
        from agents.knowledge import nodes
        from agents.knowledge.state import KnowledgeAgentState
        from agents.knowledge.types import RewrittenQuery

        async def _lookup(query, *, session):
            return THE_DUMP

        monkeypatch.setattr(nodes, "structured_lookup", _lookup)

        node = nodes.make_structured_lookup_node(session_factory=fake_session_factory())
        question = "What are the core values of the company?"
        state = KnowledgeAgentState(
            raw_query=question,
            rewritten_query=RewrittenQuery(rewritten_text=question, original_text=question),
            structured_query=UNSLOTTED,
        )

        assert (await node(state))["structured_facts"] == ()

    async def test_the_compiled_graph_falls_back_when_the_dump_is_filtered_out(
        self, monkeypatch
    ):
        """The production path, end to end: an unslotted lookup returns an
        irrelevant dump, the filter empties it, and the P1-6 edge then routes
        to semantic search — where the answer actually is.

        Worth asserting through the real graph rather than composing the two
        unit tests: P1-6 was itself a topology bug that every unit test missed.
        """
        from agents.knowledge import nodes
        from agents.knowledge.config import KnowledgeAgentConfig
        from agents.knowledge.graph import build_knowledge_agent_graph
        from agents.knowledge.types import (
            ChunkProvenance,
            GroundedResponse,
            RetrievedChunk,
            RewrittenQuery,
        )

        question = "What are the core values of the company?"
        visited: list[str] = []
        chunk = RetrievedChunk(
            chunk_id="c1", chunk_text="our core values are integrity and collaboration",
            similarity_score=0.62,
            provenance=ChunkProvenance(
                source_name="compnay_vision.pdf", source_type="EXTERNAL_INTEGRATION",
                category_name=None, version_number=1, page=None, chunk_index=7,
                entity_type=None, entity_label=None,
            ),
        )

        async def _structured(query, *, session):
            visited.append("structured_lookup")
            return THE_DUMP

        async def _vector(rewritten, *, config, session, embed_query):
            visited.append("vector_search")
            return (chunk,)

        monkeypatch.setattr(nodes, "structured_lookup", _structured)
        monkeypatch.setattr(nodes, "vector_search", _vector)
        monkeypatch.setattr(
            nodes, "rewrite_query",
            lambda *a, **k: RewrittenQuery(rewritten_text=question, original_text=question),
        )
        monkeypatch.setattr(nodes, "extract_query", lambda *a, **k: UNSLOTTED)
        monkeypatch.setattr(
            nodes, "generate_response",
            lambda prompt, *, context, llm_complete: GroundedResponse(
                answer_text="answer", is_grounded=True, citations=()
            ),
        )

        config = KnowledgeAgentConfig()
        graph = build_knowledge_agent_graph(
            config=config,
            rewrite_llm_complete=lambda p: "{}",
            extraction_llm_complete=lambda p: "{}",
            answer_llm_complete=lambda s, u: "{}",
            session_factory=fake_session_factory(),
            embed_query=lambda text: (0.0,) * config.embedding_dimension,
        )

        result = await graph.ainvoke({"raw_query": question, "conversation_history": ()})

        assert result["structured_facts"] == (), "the irrelevant dump reached the prompt"
        assert "vector_search" in visited, f"the fallback never ran; visited {visited}"
        assert "core values are integrity" in result["context"].documentation_section

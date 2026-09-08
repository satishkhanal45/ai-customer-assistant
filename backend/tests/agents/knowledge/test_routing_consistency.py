"""One question must retrieve the same way twice (F5).

`extract_query` is an LLM call, and at `temperature=0` it still returned two
different shapes for the same input on two consecutive live runs:

    run A:  entity_type='Policy',  relation_type='supports', 0.85
    run B:  entity_type='Company', no slot,                  0.62

Those routed to **structured-only** and **hybrid** — two retrieval plans, two
contexts, two different answers to one question. Tuning the confidence
threshold could never have fixed it: the runs did not differ in confidence,
they differed in *shape*, and any router keyed on shape inherits the
extractor's variance.

The fix is to stop letting an extraction switch retrieval *off*. Semantic
search now runs whenever an entity lookup does, so the two shapes converge on
the same plan. These tests assert that convergence with the extractions that
actually shipped, not with invented ones.
"""
from __future__ import annotations

import pytest

from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.constants import STRATEGY_HYBRID, STRATEGY_STRUCTURED, STRATEGY_VECTOR
from agents.knowledge.hybrid import decide_strategy
from agents.knowledge.types import StructuredQuery


@pytest.fixture
def config() -> KnowledgeAgentConfig:
    return KnowledgeAgentConfig()


# The two extractions observed live, for "Does the company support remote or
# hybrid work?", on consecutive runs at temperature=0.
RUN_A = StructuredQuery(
    entity_type="Policy", entity_label=None, attribute=None,
    relation_type="supports", filters=(), confidence=0.85,
)
RUN_B = StructuredQuery(
    entity_type="Company", entity_label="company", attribute=None,
    relation_type=None, filters=(), confidence=0.62,
)


class TestTheObservedVariance:
    def test_both_extractions_now_choose_the_same_plan(self, config):
        assert decide_strategy(RUN_A, config=config) == decide_strategy(RUN_B, config=config)

    def test_and_that_plan_includes_semantic_search(self, config):
        """Agreeing on a plan that skipped retrieval would be consistency of
        the wrong kind."""
        assert decide_strategy(RUN_A, config=config) == STRATEGY_HYBRID


class TestSemanticSearchIsNeverSwitchedOff:
    """The invariant behind the fix, stated directly."""

    def test_structured_only_is_never_selected(self, config):
        """`STRATEGY_STRUCTURED` is still implemented as a backstop for
        `hybrid_retrieve`'s callers (it carries the P1-6 fallback), but
        nothing may route to it — that is what made retrieval shape-sensitive.
        """
        assert decide_strategy(RUN_A, config=config) != STRATEGY_STRUCTURED

    @pytest.mark.parametrize("attribute", [None, "industry"])
    @pytest.mark.parametrize("relation", [None, "uses"])
    @pytest.mark.parametrize("confidence", [0.0, 0.3, 0.54, 0.55, 0.56, 0.9, 1.0])
    @pytest.mark.parametrize("entity_type", [None, "Company"])
    def test_no_combination_of_signals_selects_structured_only(
        self, config, entity_type, confidence, relation, attribute
    ):
        """Exhaustive over the shapes extraction can produce: every one of
        them must still reach the semantic index."""
        query = StructuredQuery(
            entity_type=entity_type, entity_label=None, attribute=attribute,
            relation_type=relation, filters=(), confidence=confidence,
        )
        strategy = decide_strategy(query, config=config)
        assert strategy in {STRATEGY_HYBRID, STRATEGY_VECTOR}

    @pytest.mark.parametrize("attribute", [None, "industry"], ids=["unslotted", "slotted"])
    def test_a_confidence_wobble_across_the_threshold_changes_nothing(
        self, config, attribute
    ):
        """The threshold was the second trigger for the same defect.

        Two runs landing either side of it — 0.54 and 0.56 — used to choose
        different plans for an *unslotted* extraction: vector one time,
        hybrid the next. Routing no longer consults confidence at all, so
        both shapes are covered here rather than only the slotted one, which
        happened to be immune.
        """
        import dataclasses

        below = StructuredQuery(
            entity_type="Company", entity_label=None, attribute=attribute,
            relation_type=None, filters=(),
            confidence=config.extraction_confidence_threshold - 0.01,
        )
        above = dataclasses.replace(
            below, confidence=config.extraction_confidence_threshold + 0.01
        )
        assert decide_strategy(below, config=config) == decide_strategy(above, config=config)

    def test_routing_does_not_consult_confidence_at_all(self, config):
        """Stated directly, because the property is easy to reintroduce: a
        score that varies run to run must not select a retrieval plan."""
        plans = {
            decide_strategy(
                StructuredQuery(
                    entity_type="Company", entity_label=None, attribute=None,
                    relation_type=None, filters=(), confidence=score,
                ),
                config=config,
            )
            for score in (0.0, 0.2, 0.5, 0.55, 0.6, 0.99, 1.0)
        }
        assert plans == {STRATEGY_HYBRID}


class TestTheChoiceIsRecorded:
    """The variance was invisible: `retrieval_strategy` was declared and
    documented from the start and never actually written, because LangGraph
    conditional edges cannot write state. It could only be inferred from the
    answers, which is why this took a live A/B run to notice at all."""

    async def test_the_route_node_records_the_strategy(self, config):
        from agents.knowledge.nodes import make_route_node
        from agents.knowledge.state import KnowledgeAgentState

        node = make_route_node(config=config)
        update = await node(KnowledgeAgentState(raw_query="q", structured_query=RUN_A))
        assert update == {"retrieval_strategy": STRATEGY_HYBRID}

    async def test_it_is_logged_without_customer_text(self, config, caplog):
        """P0-4 is still open — a new log line must not add to it."""
        import logging

        from agents.knowledge.nodes import make_route_node
        from agents.knowledge.state import KnowledgeAgentState

        node = make_route_node(config=config)
        with caplog.at_level(logging.INFO):
            await node(
                KnowledgeAgentState(
                    raw_query="my email is alice@example.com", structured_query=RUN_A
                )
            )

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "hybrid" in messages
        assert "alice@example.com" not in messages

    def test_the_application_actually_emits_info_logs(self, monkeypatch):
        """A log line nobody can see is not observability.

        Uvicorn leaves the root logger at WARNING, so the strategy line was
        being discarded in every deployed process — the F3 warnings came
        through and this did not, which is exactly the kind of gap that hides
        for a long time.

        Imports `logging_config`, never `main`: the entry point loads
        `backend/.env` at import, and pulling that into the test process
        makes the live-Postgres skip guards believe a database is configured.
        """
        import logging

        import logging_config

        monkeypatch.delenv("LOG_LEVEL", raising=False)
        original = logging.getLogger().level
        try:
            logging.getLogger().setLevel(logging.WARNING)
            logging_config.configure_logging()
            assert logging.getLogger().isEnabledFor(logging.INFO)
            for noisy in logging_config.NOISY_LIBRARIES:
                assert not logging.getLogger(noisy).isEnabledFor(logging.INFO), (
                    f"{noisy} at INFO narrates every model call"
                )
        finally:
            logging.getLogger().setLevel(original)

    async def test_the_compiled_graph_reports_the_strategy_it_used(
        self, monkeypatch, config
    ):
        """End to end: the field is populated in the graph's own state, which
        is what makes the decision visible in a trace or a checkpoint."""
        from agents.knowledge import nodes
        from agents.knowledge.graph import build_knowledge_agent_graph
        from agents.knowledge.types import GroundedResponse, RewrittenQuery

        question = "Does the company support remote or hybrid work?"

        async def _structured(query, *, session):
            return ()

        async def _vector(rewritten, *, config, session, embed_query):
            return ()

        monkeypatch.setattr(nodes, "structured_lookup", _structured)
        monkeypatch.setattr(nodes, "vector_search", _vector)
        monkeypatch.setattr(
            nodes, "rewrite_query",
            lambda *a, **k: RewrittenQuery(rewritten_text=question, original_text=question),
        )
        monkeypatch.setattr(nodes, "extract_query", lambda *a, **k: RUN_A)
        monkeypatch.setattr(
            nodes, "generate_response",
            lambda prompt, *, context, llm_complete: GroundedResponse(
                answer_text="answer", is_grounded=True, citations=()
            ),
        )

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        graph = build_knowledge_agent_graph(
            config=config,
            rewrite_llm_complete=lambda p: "{}",
            extraction_llm_complete=lambda p: "{}",
            answer_llm_complete=lambda s, u: "{}",
            session_factory=lambda: _Session(),
            embed_query=lambda text: (0.0,) * config.embedding_dimension,
        )
        result = await graph.ainvoke({"raw_query": question, "conversation_history": ()})

        assert result["retrieval_strategy"] == STRATEGY_HYBRID


class TestBothExtractionsProduceTheSameRetrieval:
    """Agreeing on a strategy is necessary but not sufficient — the two
    shapes must also drive the retrieval arms to the same result. They do,
    because F4 filters the unslotted dump that run B would otherwise add."""

    @pytest.mark.parametrize("query", [RUN_A, RUN_B], ids=["run-A", "run-B"])
    async def test_the_same_question_retrieves_the_same_thing(self, config, query):
        from agents.knowledge import nodes
        from agents.knowledge.fact_relevance import relevant_facts
        from agents.knowledge.types import StructuredFact

        question = "Does the company support remote or hybrid work?"

        # What the live database returns for each shape: nothing for Policy
        # (a real ontology type with no instances), an unrelated dump for the
        # general Company lookup.
        dump = (
            StructuredFact(
                entity_id="e1", entity_type="Company", entity_label="Company",
                attribute="", value="", value_type="string",
                relation_type="uses", related_entity_label="Sprint Planning",
                confidence=1.0,
            ),
        )
        facts = () if query.entity_type == "Policy" else dump

        assert relevant_facts(facts, query=query, query_text=question) == ()

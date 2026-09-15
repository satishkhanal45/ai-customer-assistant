"""A structured-only lookup that finds nothing must retry semantically (P1-6).

`decide_strategy` routes to structured-only whenever the extractor named an
entity type, asked for a specific slot, and was *confident*. When that lookup
then found nothing, retrieval ended with zero results and the customer was
told nothing was known — while the semantic index held the answer. Observed
live before the fix:

    query:       "Does the company support remote or hybrid work?"
    extraction:  entity_type='Policy', relation_type='supports', 0.85
    strategy:    structured
    structured:  0 facts   ('Policy' is a real ontology type with no instances)
    vector:      never ran (it returns the answering chunk at 0.519)

Nothing raised. The graph reported success. That is what makes this worth
testing from two directions: the shared rule, and the compiled graph that
production actually traverses — the bug lived in the *topology*, so a test of
`hybrid_retrieve` alone would have passed against the broken code.
"""
from __future__ import annotations

import pytest
from conftest import fake_session_factory

from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.constants import STRATEGY_HYBRID, STRATEGY_STRUCTURED, STRATEGY_VECTOR
from agents.knowledge.exceptions import EmptyRetrievalError, EntityNotFoundError
from agents.knowledge.hybrid import decide_strategy, should_fall_back_to_vector
from agents.knowledge.types import (
    ChunkProvenance,
    RetrievedChunk,
    RewrittenQuery,
    StructuredFact,
    StructuredQuery,
)


@pytest.fixture
def config() -> KnowledgeAgentConfig:
    return KnowledgeAgentConfig()


def _chunk(text: str = "hybrid and remote collaboration are supported") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="c1",
        chunk_text=text,
        similarity_score=0.519,
        provenance=ChunkProvenance(
            source_name="compnay_vision.pdf",
            source_type="EXTERNAL_INTEGRATION",
            category_name=None,
            version_number=1,
            page=None,
            chunk_index=3,
            entity_type=None,
            entity_label=None,
        ),
    )


def _fact() -> StructuredFact:
    return StructuredFact(
        entity_id="e1",
        entity_type="Company",
        entity_label="Alpinist Studios",
        attribute="industry",
        value="software",
        value_type="string",
        confidence=1.0,
    )


# The extraction that actually caused the bug.
THE_QUERY = StructuredQuery(
    entity_type="Policy", entity_label=None, attribute=None,
    relation_type="supports", filters=(), confidence=0.85,
)
REWRITTEN = RewrittenQuery(
    rewritten_text="Does the company support remote or hybrid work?",
    original_text="Does the company support remote or hybrid work?",
)


class TestTheRule:
    def test_empty_structured_only_falls_back(self):
        assert should_fall_back_to_vector(STRATEGY_STRUCTURED, ()) is True

    def test_structured_with_facts_does_not(self):
        assert should_fall_back_to_vector(STRATEGY_STRUCTURED, (_fact(),)) is False

    def test_hybrid_never_falls_back(self):
        """Vector search has already run alongside — falling back would
        re-run it, and in the graph that means a second superstep hitting
        the same node."""
        assert should_fall_back_to_vector(STRATEGY_HYBRID, ()) is False

    def test_vector_only_never_falls_back(self):
        assert should_fall_back_to_vector(STRATEGY_VECTOR, ()) is False

    def test_this_extraction_no_longer_routes_to_structured_only(self, config):
        """Superseded by F5, and deliberately kept as the record of it.

        This query used to take the structured-only branch — that is what
        made P1-6 reachable. F5 removed the branch entirely, because routing
        on the shape of a non-deterministic extraction meant the same
        question retrieved differently on different runs. So the fallback
        below is now a backstop rather than the production path.
        """
        assert decide_strategy(THE_QUERY, config=config) == STRATEGY_HYBRID


class TestTheRetrievalNodesDegradeGracefully:
    """These behaviours used to be asserted through `hybrid_retrieve`.

    That function is gone — it was an unused second implementation of the
    graph's own orchestration — so the same properties are now asserted on
    the nodes the graph actually runs. The behaviour is unchanged; only the
    thing being called moved onto the production path.
    """

    @staticmethod
    def _state(query=None):
        from agents.knowledge.state import KnowledgeAgentState

        return KnowledgeAgentState(
            raw_query="q", structured_query=query or THE_QUERY, rewritten_query=REWRITTEN
        )

    async def test_entity_not_found_degrades_to_no_facts(self, monkeypatch):
        """"No such entity" is the commonest way a lookup comes up empty, and
        it is what the P1-6 fallback exists for — it must not propagate."""
        from agents.knowledge import nodes

        async def _lookup(query, *, session):
            raise EntityNotFoundError(message="no Policy entities", entity_type="Policy")

        monkeypatch.setattr(nodes, "structured_lookup", _lookup)
        node = nodes.make_structured_lookup_node(session_factory=fake_session_factory())

        assert (await node(self._state()))["structured_facts"] == ()

    async def test_an_empty_vector_search_is_an_answer_not_an_error(self, monkeypatch, config):
        """The corpus genuinely having nothing is a legitimate outcome."""
        from agents.knowledge import nodes

        async def _search(rewritten, *, config, session, embed_query):
            raise EmptyRetrievalError(
                message="nothing cleared the threshold", query_text="q", top_k=8
            )

        monkeypatch.setattr(nodes, "vector_search", _search)
        node = nodes.make_vector_search_node(
            config=config, session_factory=fake_session_factory(), embed_query=lambda t: ()
        )

        assert (await node(self._state()))["retrieved_chunks"] == ()

    async def test_a_real_failure_still_propagates(self, monkeypatch):
        """Only "found nothing" degrades. An infrastructure fault must not be
        silently converted into an empty answer — that is the failure mode
        this whole class of bug is made of."""
        from agents.knowledge import nodes

        async def _lookup(query, *, session):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(nodes, "structured_lookup", _lookup)
        node = nodes.make_structured_lookup_node(session_factory=fake_session_factory())

        with pytest.raises(RuntimeError, match="connection reset"):
            await node(self._state())


class TestTheConditionalEdge:
    """The edge is what the compiled graph traverses; `hybrid_retrieve` is
    not on the production path at all."""

    def test_routes_to_vector_search_when_structured_found_nothing(self, config):
        """The strategy is declared in state (F5's `route` node writes it),
        so the backstop can still be exercised even though the graph no
        longer routes anything to structured-only."""
        from agents.knowledge.nodes import make_structured_fallback_edge
        from agents.knowledge.state import KnowledgeAgentState

        edge = make_structured_fallback_edge(config=config)
        state = KnowledgeAgentState(
            raw_query="q", structured_query=THE_QUERY, structured_facts=(),
            retrieval_strategy=STRATEGY_STRUCTURED,
        )
        assert edge(state) == "vector_search"

    def test_routes_to_rank_when_facts_were_found(self, config):
        from agents.knowledge.nodes import make_structured_fallback_edge
        from agents.knowledge.state import KnowledgeAgentState

        edge = make_structured_fallback_edge(config=config)
        state = KnowledgeAgentState(
            raw_query="q", structured_query=THE_QUERY, structured_facts=(_fact(),)
        )
        assert edge(state) == "rank"

    def test_hybrid_routes_to_rank_even_with_no_facts(self, config):
        """Hybrid reaches this same edge. Routing it to vector_search would
        run that node twice for one query."""
        from agents.knowledge.nodes import make_structured_fallback_edge
        from agents.knowledge.state import KnowledgeAgentState

        broad = StructuredQuery(
            entity_type="Company", entity_label="Alpinist Studios", attribute=None,
            relation_type=None, filters=(), confidence=0.9,
        )
        assert decide_strategy(broad, config=config) == STRATEGY_HYBRID

        edge = make_structured_fallback_edge(config=config)
        state = KnowledgeAgentState(
            raw_query="q", structured_query=broad, structured_facts=()
        )
        assert edge(state) == "rank"


class TestTheCompiledGraph:
    """End to end through the real LangGraph topology.

    This is the test that would have caught the bug. Everything below the
    routing layer was already correct — vector search returned the answering
    chunk when called — so every unit test in the package passed while the
    graph silently never called it. Only building the actual graph exercises
    the edge that was missing.

    The three LLM stages and the two retrieval functions are patched at their
    use site in `nodes`, leaving the topology itself completely real.
    """

    @staticmethod
    def _build(monkeypatch, *, facts, chunks, config):
        from agents.knowledge import nodes
        from agents.knowledge.graph import build_knowledge_agent_graph
        from agents.knowledge.types import GroundedResponse

        visited: list[str] = []

        async def _structured(query, *, session):
            visited.append("structured_lookup")
            return facts

        async def _vector(rewritten, *, config, session, embed_query):
            visited.append("vector_search")
            return chunks

        monkeypatch.setattr(nodes, "structured_lookup", _structured)
        monkeypatch.setattr(nodes, "vector_search", _vector)
        monkeypatch.setattr(nodes, "rewrite_query", lambda *a, **k: REWRITTEN)
        monkeypatch.setattr(nodes, "extract_query", lambda *a, **k: THE_QUERY)
        monkeypatch.setattr(
            nodes,
            "generate_response",
            lambda prompt, *, context, llm_complete: GroundedResponse(
                answer_text="answer", is_grounded=True, citations=()
            ),
        )

        graph = build_knowledge_agent_graph(
            config=config,
            rewrite_llm_complete=lambda p: "{}",
            extraction_llm_complete=lambda p: "{}",
            answer_llm_complete=lambda s, u: "{}",
            session_factory=fake_session_factory(),
            embed_query=lambda text: (0.0,) * config.embedding_dimension,
        )
        return graph, visited

    async def test_the_graph_reaches_vector_search_after_an_empty_lookup(
        self, monkeypatch, config
    ):
        graph, visited = self._build(
            monkeypatch, facts=(), chunks=(_chunk(),), config=config
        )

        result = await graph.ainvoke(
            {"raw_query": "Does the company support remote or hybrid work?",
             "conversation_history": ()}
        )

        assert visited == ["structured_lookup", "vector_search"], (
            "the graph must traverse structured_lookup -> vector_search; "
            f"it traversed {visited}"
        )
        assert len(result["retrieved_chunks"]) == 1
        assert result["retrieved_chunks"][0].chunk_text.startswith("hybrid and remote")

    async def test_the_answering_chunk_reaches_the_prompt(self, monkeypatch, config):
        """Retrieving it is not enough — it has to survive ranking, dedupe and
        context assembly to be of any use to the customer."""
        graph, _ = self._build(
            monkeypatch, facts=(), chunks=(_chunk(),), config=config
        )

        result = await graph.ainvoke(
            {"raw_query": "Does the company support remote or hybrid work?",
             "conversation_history": ()}
        )

        context = result["context"]
        assert "hybrid and remote" in context.documentation_section
        assert context.cited_provenance, "the chunk must be citable, not just present"

    async def test_the_graph_always_runs_semantic_search_alongside(
        self, monkeypatch, config
    ):
        """Superseded by F5, and kept as the record of the change.

        This used to assert that a successful structured lookup *skipped*
        vector search. That skip was the variance: whether a question got
        documentation depended on the shape of a non-deterministic
        extraction. Both arms now always run, concurrently, and the
        fallback edge sends hybrid straight to `rank` so vector search is
        still reached exactly once.
        """
        graph, visited = self._build(
            monkeypatch, facts=(_fact(),), chunks=(_chunk(),), config=config
        )

        await graph.ainvoke({"raw_query": "q", "conversation_history": ()})

        assert sorted(visited) == ["structured_lookup", "vector_search"]
        assert visited.count("vector_search") == 1, "vector search ran twice for one query"

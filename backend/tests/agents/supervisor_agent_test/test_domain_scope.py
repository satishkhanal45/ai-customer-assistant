"""Scope must follow the corpus, not a paragraph written once (F6).

The Supervisor decided what was in scope from `prompt.DOMAIN_DEFINITION`, a
hand-written description of the company's services with no connection at all
to the knowledge base. So it confidently refused questions it could answer:

    "Why did Soani Tech change its name?"
        -> OUT_OF_SCOPE, domain_confidence 0.95
        -> "I'm sorry, I am only able to help with solving the problem you
            are facing on our platform."

while `soani-tech-is-now-alpinist-studios` sat in the corpus.

The first fix proposed for this was to route *low-confidence* out-of-scope
classifications through retrieval. Measuring it killed the idea outright:

    "Why did Soani Tech change its name?"  -> OUT_OF_SCOPE  0.95
    "How's the weather today?"             -> OUT_OF_SCOPE  0.98

The classifier is not hesitant. It is sure, and wrong, because nobody told
it what the company had published — so no threshold could separate the two.

Measured after appending the live document titles, one classify call each:

    question                              static          corpus-aware
    Why did Soani Tech change its name?   OUT_OF_SCOPE    DOMAIN_REQUEST
    Are you hiring AI engineers?          DOMAIN_REQUEST  DOMAIN_REQUEST
    How's the weather today?              OUT_OF_SCOPE    OUT_OF_SCOPE

The last row is the one that makes this a fix rather than a capitulation:
an assistant that accepts everything is no better than one that refuses too
much.
"""
from __future__ import annotations

import logging

import pytest

from agents.supervisor.domain_scope import (
    CorpusScope,
    render_definition,
    static_scope,
)
from agents.supervisor.prompt import DOMAIN_DEFINITION

LIVE_SOURCES = [
    "soani-tech-is-now-alpinist-studios",
    "senior-artificial-intelligence-ai-engineer",
    "pricing.pdf",
    "company_overview.pdf",
]


class _Rows:
    def __init__(self, names):
        self._names = names

    def all(self):
        return [(name,) for name in self._names]


class _Session:
    def __init__(self, names=None, error=None):
        self._names = names or []
        self._error = error
        self.queries = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.queries += 1
        if self._error:
            raise self._error
        return _Rows(self._names)


def _factory(session):
    return lambda: session


class TestTheDefinitionCarriesTheCorpus:
    def test_the_document_the_assistant_refused_is_named(self):
        definition = render_definition(DOMAIN_DEFINITION, LIVE_SOURCES)
        assert "soani-tech-is-now-alpinist-studios" in definition

    def test_the_original_description_is_kept(self):
        """Widening scope must not throw away the hand-written description —
        it is what keeps genuinely unrelated questions out."""
        definition = render_definition(DOMAIN_DEFINITION, LIVE_SOURCES)
        assert definition.startswith(DOMAIN_DEFINITION)

    def test_an_empty_corpus_changes_nothing(self):
        assert render_definition(DOMAIN_DEFINITION, []) == DOMAIN_DEFINITION


class TestItTracksIngestion:
    async def test_the_titles_are_read_from_the_database(self):
        session = _Session(LIVE_SOURCES)
        scope = CorpusScope()
        assert scope.current() == DOMAIN_DEFINITION

        await scope.refresh_if_stale(_factory(session))

        assert "soani-tech-is-now-alpinist-studios" in scope.current()

    async def test_a_fresh_snapshot_is_not_requeried(self):
        """One query per TTL, not one per turn."""
        session = _Session(LIVE_SOURCES)
        scope = CorpusScope(ttl_seconds=300.0)

        await scope.refresh_if_stale(_factory(session))
        await scope.refresh_if_stale(_factory(session))
        await scope.refresh_if_stale(_factory(session))

        assert session.queries == 1

    async def test_an_expired_snapshot_is_re_read(self):
        """A definition frozen at startup would refuse questions about a
        document the worker had just ingested — the same bug, delayed."""
        session = _Session(["pricing.pdf"])
        scope = CorpusScope(ttl_seconds=0.0)

        await scope.refresh_if_stale(_factory(session))
        session._names = ["pricing.pdf", "a-brand-new-document"]
        await scope.refresh_if_stale(_factory(session))

        assert "a-brand-new-document" in scope.current()

    def test_scope_matches_the_retrieval_join_contract_exactly(self):
        """The classifier must not admit a question about a document that
        vector search is contractually forbidden to return.

        Checked against the compiled SQL, because the first version of this
        query got it wrong in a way no unit test noticed: it filtered on
        active + current version but not on INDEXED, so 21 sources qualified
        where retrieval would only ever surface 9. The live log caught it.
        """
        from agents.supervisor.domain_scope import live_sources_statement

        sql = str(live_sources_statement(10).compile(compile_kwargs={"literal_binds": True}))
        collapsed = " ".join(sql.split()).lower()

        assert "knowledge_source.is_active" in collapsed
        assert "current_version_id" in collapsed
        assert "indexed" in collapsed, (
            "without the INDEXED filter the classifier accepts questions "
            "retrieval cannot answer"
        )


class TestItSurvivesADatabaseOutage:
    async def test_a_failure_keeps_the_previous_definition(self):
        """A classifier that stops working because the knowledge base is
        briefly unreachable would be worse than the bug being fixed."""
        scope = CorpusScope(ttl_seconds=0.0)
        await scope.refresh_if_stale(_factory(_Session(LIVE_SOURCES)))
        good = scope.current()

        await scope.refresh_if_stale(_factory(_Session(error=RuntimeError("down"))))

        assert scope.current() == good

    async def test_a_failure_before_any_success_falls_back_to_the_paragraph(self):
        scope = CorpusScope(ttl_seconds=0.0)
        await scope.refresh_if_stale(_factory(_Session(error=RuntimeError("down"))))
        assert scope.current() == DOMAIN_DEFINITION

    async def test_a_failure_is_logged_rather_than_swallowed(self, caplog):
        scope = CorpusScope(ttl_seconds=0.0)
        with caplog.at_level(logging.WARNING):
            await scope.refresh_if_stale(_factory(_Session(error=RuntimeError("down"))))
        assert caplog.records

    async def test_a_down_database_is_not_queried_every_turn(self):
        """The TTL is pushed out even on failure, so an outage costs one
        attempt per interval rather than one per message."""
        session = _Session(error=RuntimeError("down"))
        scope = CorpusScope(ttl_seconds=300.0)

        await scope.refresh_if_stale(_factory(session))
        await scope.refresh_if_stale(_factory(session))

        assert session.queries == 1

    async def test_a_static_scope_never_queries_anything(self):
        session = _Session(LIVE_SOURCES)
        scope = static_scope()
        await scope.refresh_if_stale(_factory(session))
        assert session.queries == 0
        assert scope.current() == DOMAIN_DEFINITION


class TestTheClassifierReceivesIt:
    def test_the_node_asks_for_the_definition_on_every_turn(self):
        """Called per turn, not captured at graph-build time — otherwise the
        TTL refresh would never reach the classifier."""
        from agents.supervisor.node import make_classify_and_route_node

        seen = []

        class _Client:
            def classify(self, system_prompt, user_message, conversation_history):
                seen.append(system_prompt)
                return '{"request_category":"OUT_OF_SCOPE","domain_confidence":0.9,' \
                       '"intent":"UNKNOWN","intent_confidence":0.0,"clarification_question":null}'

        definitions = iter(["FIRST DEFINITION", "SECOND DEFINITION"])
        node = make_classify_and_route_node(_Client(), domain_definition=lambda: next(definitions))

        node({"user_message": "a", "conversation_history": []})
        node({"user_message": "b", "conversation_history": []})

        assert "FIRST DEFINITION" in seen[0]
        assert "SECOND DEFINITION" in seen[1]

    def test_the_default_is_the_static_paragraph(self):
        from agents.supervisor.node import make_classify_and_route_node

        seen = []

        class _Client:
            def classify(self, system_prompt, user_message, conversation_history):
                seen.append(system_prompt)
                return '{"request_category":"GREETING","domain_confidence":0.9,' \
                       '"intent":"UNKNOWN","intent_confidence":0.0,"clarification_question":null}'

        make_classify_and_route_node(_Client())({"user_message": "hi", "conversation_history": []})
        assert DOMAIN_DEFINITION in seen[0]

    def test_the_graph_wires_it_through(self):
        """A provider that is accepted and then dropped would leave the bug
        in place while every unit test passed."""
        from langgraph.checkpoint.memory import MemorySaver

        from agents.supervisor.graph import build_supervisor_graph

        seen = []

        class _Client:
            def classify(self, system_prompt, user_message, conversation_history):
                seen.append(system_prompt)
                return '{"request_category":"OUT_OF_SCOPE","domain_confidence":0.9,' \
                       '"intent":"UNKNOWN","intent_confidence":0.0,"clarification_question":null}'

        graph = build_supervisor_graph(
            llm_client=_Client(),
            checkpointer=MemorySaver(),
            domain_definition=lambda: "CORPUS AWARE DEFINITION",
        )
        graph.invoke(
            {"user_message": "hello", "conversation_history": []},
            config={"configurable": {"thread_id": "scope-1"}},
        )
        assert seen and "CORPUS AWARE DEFINITION" in seen[0]

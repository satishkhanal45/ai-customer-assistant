"""Smoke test for the LangGraph Studio builder.

This is a guard, not a re-run of the Studio verification (which was done
manually through the local runs API). The point is to fail loudly if the
Studio builder's compiled graph drifts from the expected topology — e.g. a
renamed node, a removed node, or a ``build_supervisor_graph`` signature
change that silently broke the wiring. It intentionally avoids ``ainvoke``.
"""
from __future__ import annotations

from agents.supervisor.studio import build_supervisor_graph_for_studio

EXPECTED_NODES = frozenset(
    {
        "classify_and_route",
        "knowledge_agent",
        "safety_gate",
        "ticket_agent",
        "assemble_response",
    }
)


def test_studio_graph_node_set():
    graph = build_supervisor_graph_for_studio()
    actual = frozenset(
        node for node in graph.get_graph().nodes if not node.startswith("__")
    )
    assert actual == EXPECTED_NODES
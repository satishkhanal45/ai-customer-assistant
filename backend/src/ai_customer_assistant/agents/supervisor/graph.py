"""Entry point: builds and compiles the Supervisor's LangGraph graph.

Knowledge Agent and Ticket Agent are out of scope for this module. They are
wired in only as named placeholder nodes so the graph is structurally
complete end-to-end; swap _placeholder_agent_node's registration for the
real agent modules once they exist.
"""
from __future__ import annotations

from typing import Optional

from langgraph.graph import END, StateGraph

from .llm_client import StubSupervisorLLMClient, SupervisorLLMClient
from .node import assemble_response_node, make_classify_and_route_node
from .schema import NextAgent, SupervisorState

CLASSIFY_NODE = "classify_and_route"
ASSEMBLE_NODE = "assemble_response"
KNOWLEDGE_AGENT_NODE = "knowledge_agent"
TICKET_AGENT_NODE = "ticket_agent"

# Where classification/post-downstream routing can send the conversation.
_ROUTE_TARGETS = {
    NextAgent.KNOWLEDGE_AGENT: KNOWLEDGE_AGENT_NODE,
    NextAgent.TICKET_AGENT: TICKET_AGENT_NODE,
    NextAgent.NONE: END,
}


def _placeholder_agent_node(agent_name: str):
    """Stand-in for an out-of-scope downstream agent.

    Returns a passthrough downstream_result so the graph remains runnable
    until Knowledge Agent / Ticket Agent are implemented as their own
    modules under agents/.
    """

    def _run(_: SupervisorState) -> dict:
        return {
            "downstream_result": {
                "status": "NOT_IMPLEMENTED",
                "response": f"{agent_name} is not yet implemented.",
            }
        }

    return _run


def _route_after_classification(state: SupervisorState) -> str:
    """Conditional-edge function: clarification short-circuits straight to
    END (the question is the response); otherwise dispatch on next_agent.
    """
    return END if state.get("clarification_required", False) else _ROUTE_TARGETS.get(
        state.get("next_agent"), END
    )


def build_supervisor_graph(llm_client: Optional[SupervisorLLMClient] = None):
    """Construct and compile the Supervisor's LangGraph graph.

    Pass a real SupervisorLLMClient implementation (backed by config.py's
    provider settings) once one exists; defaults to the deterministic stub.
    """
    client = llm_client or StubSupervisorLLMClient()

    graph = StateGraph(SupervisorState)
    graph.add_node(CLASSIFY_NODE, make_classify_and_route_node(client))
    graph.add_node(KNOWLEDGE_AGENT_NODE, _placeholder_agent_node("Knowledge Agent"))
    graph.add_node(TICKET_AGENT_NODE, _placeholder_agent_node("Ticket Agent"))
    graph.add_node(ASSEMBLE_NODE, assemble_response_node)

    graph.set_entry_point(CLASSIFY_NODE)
    graph.add_conditional_edges(CLASSIFY_NODE, _route_after_classification)
    graph.add_edge(KNOWLEDGE_AGENT_NODE, ASSEMBLE_NODE)
    graph.add_edge(TICKET_AGENT_NODE, ASSEMBLE_NODE)
    graph.add_edge(ASSEMBLE_NODE, END)

    return graph.compile()
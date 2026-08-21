"""Entry point: builds and compiles the Supervisor's LangGraph graph.

Composition style (per agents_integration_plan_new.md §4.0): downstream
agents are injected as already-built node callables (or compiled
subgraphs wrapped by their adapter), never constructed here. When no
dependency is supplied, a placeholder node keeps the graph structurally
complete end-to-end so classification/routing can be tested in
isolation from the real agents.

A compiled graph may optionally receive a checkpointer (e.g.
`MemorySaver` in dev) so state keyed by `thread_id` can be resumed —
the mechanism the ticket-email and escalation-confirmation flows use
later (see §4.3).
"""
from __future__ import annotations

import inspect
import json
from typing import Callable, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from .agents_wiring import (
    make_knowledge_agent_node,
    make_ticket_agent_node,
)
from .llm_client import StubSupervisorLLMClient, SupervisorLLMClient
from .node import assemble_response_node, make_classify_and_route_node
from .schema import NextAgent, SupervisorState
from ..ticket_agent.store import TicketStore

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


def _jsonable(value: object) -> object:
    """Best-effort JSON conversion so any state value (dataclasses, enums,
    provenance objects) can be dumped for terminal debugging."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def _log_node(node_name: str, node_fn):
    """Wrap a node function to print the SupervisorState on entry and the
    partial update it returns — a dev/debug aid that works for both sync and
    async node functions. ``interrupt()`` pauses propagate untouched."""
    if inspect.iscoroutinefunction(node_fn):

        async def _async_logged(state: SupervisorState) -> dict:
            print(f"\n--- supervisor node: {node_name} (in) ---")
            print(json.dumps(_jsonable(state), indent=2))
            result = await node_fn(state)
            print(f"--- supervisor node: {node_name} (out) ---")
            print(json.dumps(_jsonable(result), indent=2))
            return result

        return _async_logged

    def _sync_logged(state: SupervisorState) -> dict:
        print(f"\n--- supervisor node: {node_name} (in) ---")
        print(json.dumps(_jsonable(state), indent=2))
        result = node_fn(state)
        print(f"--- supervisor node: {node_name} (out) ---")
        print(json.dumps(_jsonable(result), indent=2))
        return result

    return _sync_logged


def _placeholder_agent_node(agent_name: str):
    """Stand-in for a not-yet-injected downstream agent.

    Returns a passthrough downstream_result so the graph remains runnable
    until the real adapter node for the agent is injected by the caller.
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


def build_supervisor_graph(
    llm_client: Optional[SupervisorLLMClient] = None,
    *,
    knowledge_agent_node: Optional[Callable[[SupervisorState], dict]] = None,
    ticket_agent_node: Optional[Callable[[SupervisorState], dict]] = None,
    knowledge_graph: Optional[Callable] = None,
    knowledge_timeout_s: float = 120,
    ticket_ops: Optional[Callable] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    """Construct and compile the Supervisor's LangGraph graph.

    Pass a real SupervisorLLMClient implementation (backed by config.py's
    provider settings) once one exists; defaults to the deterministic
    stub. Downstream agent nodes and the checkpointer are optional
    injected dependencies; None uses the placeholder/unchecked default.

    Phase 1: pass the compiled Knowledge Agent graph via ``knowledge_graph``
    (wrapped by ``make_knowledge_agent_node``) to replace the Knowledge
    placeholder with the real RAG subgraph. Passing both ``knowledge_graph``
    and an explicit ``knowledge_agent_node`` is ambiguous and raises
    ValueError — the callable must not silently override a wired graph.

    Every downstream agent (Knowledge or Ticket) reports back a
    ``downstream_result``; ``assemble_response`` is pure string formatting
    that renders it into ``final_response``. The Knowledge Agent's answer is
    passed straight through — there is no separate safety gate.
    """
    client = llm_client or StubSupervisorLLMClient()

    if knowledge_agent_node is not None and knowledge_graph is not None:
        raise ValueError(
            "Pass either knowledge_agent_node or a knowledge_graph API callable as the "
            "Knowledge source name, not both: an explicit node callable must not "
            "silently override the wired knowledge path. Use knowledge_agent_node "
            "for raw injection at graph-build time, and knowledge_graph for the "
            "real subgraph; providing both is ambiguous."
        )

    graph = StateGraph(SupervisorState)
    graph.add_node(
        CLASSIFY_NODE, _log_node(CLASSIFY_NODE, make_classify_and_route_node(client))
    )
    graph.add_node(
        KNOWLEDGE_AGENT_NODE,
        _log_node(
            KNOWLEDGE_AGENT_NODE,
            knowledge_agent_node
            or (
                make_knowledge_agent_node(knowledge_graph, timeout_s=knowledge_timeout_s)
                if knowledge_graph is not None
                else _placeholder_agent_node("Knowledge Agent")
            ),
        ),
    )
    graph.add_node(
        TICKET_AGENT_NODE,
        _log_node(
            TICKET_AGENT_NODE,
            ticket_agent_node
            or make_ticket_agent_node(
                ticket_ops if ticket_ops is not None else TicketStore()
            ),
        ),
    )
    graph.add_node(
        ASSEMBLE_NODE, _log_node(ASSEMBLE_NODE, assemble_response_node)
    )

    graph.set_entry_point(CLASSIFY_NODE)
    graph.add_conditional_edges(CLASSIFY_NODE, _route_after_classification)
    graph.add_edge(KNOWLEDGE_AGENT_NODE, ASSEMBLE_NODE)
    graph.add_edge(TICKET_AGENT_NODE, ASSEMBLE_NODE)
    graph.add_edge(ASSEMBLE_NODE, END)

    return graph.compile(checkpointer=checkpointer)
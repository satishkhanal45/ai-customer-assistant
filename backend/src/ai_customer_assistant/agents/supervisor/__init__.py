"""Supervisor agent: domain gate, intent classification, and routing.

Public API re-exported here; internal modules (classification, routing,
node, llm_client) are wired together by graph.build_supervisor_graph.
"""
from .classification import Classification, parse_llm_response
from .graph import build_supervisor_graph
from .llm_client import StubSupervisorLLMClient, SupervisorLLMClient
from .routing import RoutingDecision, decide_post_downstream, decide_route
from .schema import (
    ConversationTurn,
    Intent,
    NextAgent,
    RequestCategory,
    SupervisorState,
    TicketType,
)

__all__ = [
    "Classification",
    "parse_llm_response",
    "build_supervisor_graph",
    "StubSupervisorLLMClient",
    "SupervisorLLMClient",
    "RoutingDecision",
    "decide_post_downstream",
    "decide_route",
    "ConversationTurn",
    "Intent",
    "NextAgent",
    "RequestCategory",
    "SupervisorState",
    "TicketType",
]
"""Data layer for the Supervisor agent.

Only shape and constants live here: enums, the LangGraph state definition,
and confidence/retry thresholds. No business logic, no I/O.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, TypedDict


class RequestCategory(str, Enum):
    GREETING = "GREETING"
    DOMAIN_REQUEST = "DOMAIN_REQUEST"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class Intent(str, Enum):
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    CREATE_TICKET = "CREATE_TICKET"
    CHECK_TICKET_STATUS = "CHECK_TICKET_STATUS"
    UNKNOWN = "UNKNOWN"


class NextAgent(str, Enum):
    KNOWLEDGE_AGENT = "KNOWLEDGE_AGENT"
    TICKET_AGENT = "TICKET_AGENT"
    NONE = "NONE"


class TicketType(str, Enum):
    STANDARD = "STANDARD"
    ESCALATION = "ESCALATION"


class ConfidenceTier(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# Confidence tiering. Illustrative cutoffs discussed alongside the prompt;
# adjust here in one place if a real classifier's score distribution differs.
HIGH_CONFIDENCE_THRESHOLD = 0.8
MEDIUM_CONFIDENCE_THRESHOLD = 0.5

# UNKNOWN intent gets N clarification attempts before Supervisor reroutes
# the conversation into Ticket Agent as an escalation, rather than looping
# on clarification forever.
MAX_CLARIFICATION_ATTEMPTS = 3


class ConversationTurn(TypedDict):
    role: str
    content: str


class SupervisorState(TypedDict, total=False):
    """LangGraph state channel for the Supervisor agent.

    Nodes never mutate this in place — each node returns a partial dict of
    updates, and LangGraph merges it into a new state. Fields are grouped
    below by ownership, matching the design discussion:

    - Inputs: read by Supervisor, owned by the graph/caller.
    - Supervisor-owned: the only fields Supervisor nodes may set.
    """

    # --- inputs, not owned by Supervisor ---
    user_message: str
    conversation_history: list[ConversationTurn]
    downstream_result: Optional[dict]

    # --- Supervisor-owned fields ---
    request_category: Optional[RequestCategory]
    domain_confidence: Optional[float]
    intent: Optional[Intent]
    intent_confidence: Optional[float]
    clarification_required: bool
    clarification_question: Optional[str]
    clarification_attempts: int
    next_agent: Optional[NextAgent]
    ticket_type: Optional[TicketType]
    final_response: Optional[str]
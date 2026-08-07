"""Orchestration layer: LangGraph node functions for the Supervisor agent.

This is the only module in the package that performs I/O (an LLM call).
It stays thin by design: call the client, parse, decide, return a partial
state update. All actual decisions are delegated to the pure functions in
classification.py and routing.py.
"""
from __future__ import annotations

from typing import Callable

from .classification import parse_llm_response
from .llm_client import SupervisorLLMClient
from .prompt import SUPERVISOR_SYSTEM_PROMPT
from .routing import decide_post_downstream, decide_route
from .schema import SupervisorState


def make_classify_and_route_node(
    llm_client: SupervisorLLMClient,
) -> Callable[[SupervisorState], dict]:
    """Build the classify-and-route node, closing over the LLM client.

    The returned function is what LangGraph invokes on entry: it reads
    user_message / conversation_history, classifies, decides where to route,
    and returns a partial update — never a mutated copy of the input state.
    """

    def classify_and_route(state: SupervisorState) -> dict:
        raw_response = llm_client.classify(
            system_prompt=SUPERVISOR_SYSTEM_PROMPT,
            user_message=state["user_message"],
            conversation_history=state.get("conversation_history", []),
        )
        classification = parse_llm_response(raw_response)
        decision = decide_route(
            classification=classification,
            prior_attempts=state.get("clarification_attempts", 0),
        )

        return {
            "request_category": classification.request_category,
            "domain_confidence": classification.domain_confidence,
            "intent": classification.intent,
            "intent_confidence": classification.intent_confidence,
            "clarification_required": decision.clarification_required,
            "clarification_question": decision.clarification_question,
            "clarification_attempts": decision.clarification_attempts,
            "next_agent": decision.next_agent,
            "ticket_type": decision.ticket_type,
            "final_response": decision.final_response,
        }

    return classify_and_route


def assemble_response_node(state: SupervisorState) -> dict:
    """Run after a downstream agent (Knowledge Agent / Ticket Agent) reports
    back. Either finalizes the response or reroutes into Ticket Agent as an
    escalation (KA-ungrounded + customer confirmed they want human help).
    """
    downstream_result = state.get("downstream_result") or {}
    decision = decide_post_downstream(downstream_result)

    return {
        "next_agent": decision.next_agent,
        "ticket_type": decision.ticket_type,
        "final_response": decision.final_response,
    }
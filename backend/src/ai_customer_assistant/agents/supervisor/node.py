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
from .prompt import build_supervisor_system_prompt
from .routing import _SAFE_FALLBACK_RESPONSE, assemble_final_response, decide_route
from .schema import ConversationTurn, NextAgent, SupervisorState

_MAX_HISTORY_TURNS = 4
_MAX_HISTORY_CHARS = 4000


def _bounded_history(history: list[ConversationTurn]) -> list[ConversationTurn]:
    """Keep the tail of the conversation (last few turns, char-capped).

    Long histories bloat the classify prompt and make Groq's
    ``json_object`` output flaky, which previously surfaced as spurious
    OUT_OF_SCOPE declines once a conversation accumulated several long
    grounded answers.
    """
    turns = history[-_MAX_HISTORY_TURNS:]
    kept: list[ConversationTurn] = []
    total = 0
    for turn in turns:
        if total + len(turn.content) > _MAX_HISTORY_CHARS:
            break
        kept.append(turn)
        total += len(turn.content)
    return kept


def _transient_error_update(state: SupervisorState) -> dict:
    """Classify LLM unavailable: never decline an in-domain question as
    OUT_OF_SCOPE because the model failed. Surface a transient error so the
    customer can retry, mirroring the Knowledge node's error path."""
    classification = parse_llm_response("{}")
    return {
        "request_category": classification.request_category,
        "domain_confidence": classification.domain_confidence,
        "intent": classification.intent,
        "intent_confidence": classification.intent_confidence,
        "clarification_required": False,
        "clarification_question": None,
        "clarification_attempts": state.get("clarification_attempts", 0),
        "next_agent": NextAgent.NONE,
        "ticket_type": None,
        "final_response": _SAFE_FALLBACK_RESPONSE,
    }


def make_classify_and_route_node(
    llm_client: SupervisorLLMClient,
) -> Callable[[SupervisorState], dict]:
    """Build the classify-and-route node, closing over the LLM client.

    The returned function is what LangGraph invokes on entry: it reads
    user_message / conversation_history, classifies, decides where to route,
    and returns a partial update — never a mutated copy of the input state.
    """

    def classify_and_route(state: SupervisorState) -> dict:
        try:
            raw_response = llm_client.classify(
                system_prompt=build_supervisor_system_prompt(),
                user_message=state["user_message"],
                conversation_history=_bounded_history(
                    state.get("conversation_history", [])
                ),
            )
        except Exception:
            return _transient_error_update(state)

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
    """Run at the FINALIZE end of the post-downstream conditional edge.

    Reduced to pure string formatting since Phase 3: the routing decision
    (finalize vs. escalate into Ticket Agent) has moved out of this node
    into the ``_route_after_downstream`` conditional edge in ``graph.py``.
    All it does is render ``downstream_result["response"]`` into
    ``final_response`` (with the safe fallback if absent/empty).
    """
    return {"final_response": assemble_final_response(state.get("downstream_result"))}
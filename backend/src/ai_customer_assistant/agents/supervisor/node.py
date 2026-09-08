"""Orchestration layer: LangGraph node functions for the Supervisor agent.

This is the only module in the package that performs I/O (an LLM call).
It stays thin by design: call the client, parse, decide, return a partial
state update. All actual decisions are delegated to the pure functions in
classification.py and routing.py.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .classification import parse_llm_response
from .llm_client import SupervisorLLMClient
from .prompt import DOMAIN_DEFINITION, build_supervisor_system_prompt
from .routing import (
    _SAFE_FALLBACK_RESPONSE,
    assemble_final_response,
    decide_route,
    failure_response,
)
from .schema import ConversationTurn, NextAgent, SupervisorState

logger = logging.getLogger(__name__)

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


def _transient_error_update(
    state: SupervisorState, exc: Optional[BaseException] = None
) -> dict:
    """Classify LLM unavailable: never decline an in-domain question as
    OUT_OF_SCOPE because the model failed. Surface a transient error so the
    customer can retry, mirroring the Knowledge node's error path.

    ``exc`` selects the wording: a provider rate limit is a wait, not a
    breakage, and saying so gives the customer something to act on."""
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
        "final_response": failure_response(exc),
    }


def make_classify_and_route_node(
    llm_client: SupervisorLLMClient,
    domain_definition: Optional[Callable[[], str]] = None,
) -> Callable[[SupervisorState], dict]:
    """Build the classify-and-route node, closing over the LLM client.

    The returned function is what LangGraph invokes on entry: it reads
    user_message / conversation_history, classifies, decides where to route,
    and returns a partial update — never a mutated copy of the input state.

    ``domain_definition`` supplies what counts as in-scope, called fresh on
    each turn so it can track the knowledge base (F6). It defaults to the
    static paragraph, which is what made the classifier confidently refuse
    questions the corpus answered — see ``domain_scope``.
    """
    resolve_domain = domain_definition or (lambda: DOMAIN_DEFINITION)

    def classify_and_route(state: SupervisorState) -> dict:
        try:
            raw_response = llm_client.classify(
                system_prompt=build_supervisor_system_prompt(resolve_domain()),
                user_message=state["user_message"],
                conversation_history=_bounded_history(
                    state.get("conversation_history", [])
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a failed classify must
            # never take the whole turn down, but it must not vanish either.
            # This branch used to swallow the exception silently, so a Groq
            # 429 and a genuine bug produced the same customer-facing apology
            # and left nothing at all in the log to tell them apart.
            logger.warning(
                "supervisor classification failed (%s); returning a transient "
                "error to the customer",
                type(exc).__name__,
                exc_info=True,
            )
            return _transient_error_update(state, exc)

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
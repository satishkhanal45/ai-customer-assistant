"""Business logic layer: pure routing decisions for the Supervisor agent.

Every function here is total and side-effect free — given the same inputs
it always produces the same RoutingDecision. Branching is expressed as
dispatch tables keyed by category/case rather than if/elif chains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .classification import Classification
from .schema import (
    HIGH_CONFIDENCE_THRESHOLD,
    MAX_CLARIFICATION_ATTEMPTS,
    MEDIUM_CONFIDENCE_THRESHOLD,
    ConfidenceTier,
    Intent,
    NextAgent,
    RequestCategory,
    TicketType,
)


@dataclass(frozen=True)
class RoutingDecision:
    next_agent: NextAgent
    clarification_required: bool
    clarification_question: Optional[str]
    clarification_attempts: int
    ticket_type: Optional[TicketType]
    final_response: Optional[str]


_GENERIC_CLARIFICATION = (
    "I can help with creating a ticket, checking ticket status, or "
    "answering questions from our knowledge base — which of these do you need?"
)

_DECLINE_RESPONSE = (
    "I'm sorry, that's outside what I can help with here. I'm able to "
    "assist with support tickets and questions about our products, "
    "services, and policies."
)

_SAFE_FALLBACK_RESPONSE = (
    "Sorry, something went wrong on my end. Let me connect you with a "
    "human representative."
)


def confidence_tier(confidence: float) -> ConfidenceTier:
    """Map a raw confidence score onto HIGH / MEDIUM / LOW."""
    tiers = (
        (HIGH_CONFIDENCE_THRESHOLD, ConfidenceTier.HIGH),
        (MEDIUM_CONFIDENCE_THRESHOLD, ConfidenceTier.MEDIUM),
    )
    return next(
        (tier for threshold, tier in tiers if confidence >= threshold),
        ConfidenceTier.LOW,
    )


def assemble_final_response(downstream_result: Optional[dict]) -> str:
    """Pass through a downstream agent's response, with a safe fallback."""
    return (downstream_result or {}).get("response") or _SAFE_FALLBACK_RESPONSE


# ---------------------------------------------------------------------------
# GREETING / OUT_OF_SCOPE — terminal, no downstream agent involved.
# ---------------------------------------------------------------------------

def _greeting_decision(_: Classification, __: int) -> RoutingDecision:
    return RoutingDecision(
        next_agent=NextAgent.NONE,
        clarification_required=False,
        clarification_question=None,
        clarification_attempts=0,
        ticket_type=None,
        final_response="Hello! How can I help you today?",
    )


def _out_of_scope_decision(_: Classification, __: int) -> RoutingDecision:
    return RoutingDecision(
        next_agent=NextAgent.NONE,
        clarification_required=False,
        clarification_question=None,
        clarification_attempts=0,
        ticket_type=None,
        final_response=_DECLINE_RESPONSE,
    )


# ---------------------------------------------------------------------------
# DOMAIN_REQUEST — escalate / clarify / route, dispatched by case.
# ---------------------------------------------------------------------------

_INTENT_TO_AGENT = {
    Intent.KNOWLEDGE_QUERY: NextAgent.KNOWLEDGE_AGENT,
    Intent.CREATE_TICKET: NextAgent.TICKET_AGENT,
    Intent.CHECK_TICKET_STATUS: NextAgent.TICKET_AGENT,
}

_INTENT_TO_TICKET_TYPE = {
    Intent.CREATE_TICKET: TicketType.STANDARD,
    Intent.CHECK_TICKET_STATUS: TicketType.STANDARD,
}


def _build_escalation_decision(_: Classification, prior_attempts: int) -> RoutingDecision:
    """UNKNOWN intent that survived MAX_CLARIFICATION_ATTEMPTS rounds:
    reroute into Ticket Agent as an escalation instead of looping forever.
    """
    return RoutingDecision(
        next_agent=NextAgent.TICKET_AGENT,
        clarification_required=False,
        clarification_question=None,
        clarification_attempts=prior_attempts + 1,
        ticket_type=TicketType.ESCALATION,
        final_response=None,
    )


def _build_clarification_decision(
    classification: Classification, prior_attempts: int
) -> RoutingDecision:
    question = classification.clarification_question or _GENERIC_CLARIFICATION
    return RoutingDecision(
        next_agent=NextAgent.NONE,
        clarification_required=True,
        clarification_question=question,
        clarification_attempts=prior_attempts + 1,
        ticket_type=None,
        final_response=None,
    )


def _build_routed_decision(
    classification: Classification, _: int
) -> RoutingDecision:
    return RoutingDecision(
        next_agent=_INTENT_TO_AGENT.get(classification.intent, NextAgent.TICKET_AGENT),
        clarification_required=False,
        clarification_question=None,
        clarification_attempts=0,
        ticket_type=_INTENT_TO_TICKET_TYPE.get(classification.intent),
        final_response=None,
    )


_CASE_BUILDERS: dict[str, Callable[[Classification, int], RoutingDecision]] = {
    "ESCALATE": _build_escalation_decision,
    "CLARIFY": _build_clarification_decision,
    "ROUTE": _build_routed_decision,
}


def _decision_case(needs_clarification: bool, exhausted: bool) -> str:
    return next(
        case
        for predicate, case in (
            (exhausted, "ESCALATE"),
            (needs_clarification, "CLARIFY"),
            (True, "ROUTE"),
        )
        if predicate
    )


def _domain_request_decision(
    classification: Classification, prior_attempts: int
) -> RoutingDecision:
    is_unknown = classification.intent is Intent.UNKNOWN
    tier = confidence_tier(classification.intent_confidence)
    needs_clarification = is_unknown or tier is not ConfidenceTier.HIGH
    exhausted = is_unknown and (prior_attempts + 1) >= MAX_CLARIFICATION_ATTEMPTS

    case = _decision_case(needs_clarification, exhausted)
    return _CASE_BUILDERS[case](classification, prior_attempts)


# ---------------------------------------------------------------------------
# Top-level dispatch by request_category.
# ---------------------------------------------------------------------------

_CATEGORY_BUILDERS: dict[RequestCategory, Callable[[Classification, int], RoutingDecision]] = {
    RequestCategory.GREETING: _greeting_decision,
    RequestCategory.OUT_OF_SCOPE: _out_of_scope_decision,
    RequestCategory.DOMAIN_REQUEST: _domain_request_decision,
}


def decide_route(classification: Classification, prior_attempts: int) -> RoutingDecision:
    """Compute the Supervisor's routing decision for a fresh classification."""
    builder = _CATEGORY_BUILDERS.get(classification.request_category, _out_of_scope_decision)
    return builder(classification, prior_attempts)


# ---------------------------------------------------------------------------
# Post-downstream dispatch — after Knowledge Agent / Ticket Agent report back.
# ---------------------------------------------------------------------------

def _build_post_escalation(_: dict) -> RoutingDecision:
    """Knowledge Agent reported ungrounded and the customer confirmed they
    want human help: reroute into Ticket Agent as an escalation.
    """
    return RoutingDecision(
        next_agent=NextAgent.TICKET_AGENT,
        clarification_required=False,
        clarification_question=None,
        clarification_attempts=0,
        ticket_type=TicketType.ESCALATION,
        final_response=None,
    )


def _build_post_finalize(downstream_result: dict) -> RoutingDecision:
    return RoutingDecision(
        next_agent=NextAgent.NONE,
        clarification_required=False,
        clarification_question=None,
        clarification_attempts=0,
        ticket_type=None,
        final_response=assemble_final_response(downstream_result),
    )


_POST_DOWNSTREAM_BUILDERS: dict[str, Callable[[dict], RoutingDecision]] = {
    "ESCALATE": _build_post_escalation,
    "FINALIZE": _build_post_finalize,
}


def decide_post_downstream(downstream_result: dict) -> RoutingDecision:
    """Decide what happens after a downstream agent (KA/TA) reports back."""
    is_ungrounded = downstream_result.get("status") == "UNGROUNDED"
    wants_escalation = bool(downstream_result.get("customer_wants_escalation"))
    case = "ESCALATE" if is_ungrounded and wants_escalation else "FINALIZE"
    return _POST_DOWNSTREAM_BUILDERS[case](downstream_result)
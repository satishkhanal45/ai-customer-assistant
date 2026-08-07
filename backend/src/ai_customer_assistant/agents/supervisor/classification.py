"""Business logic layer: turn raw LLM output into a typed, validated result.

Every function here is pure and total: no I/O, no mutation, and malformed
input degrades to a safe fallback rather than raising.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .schema import Intent, RequestCategory


@dataclass(frozen=True)
class Classification:
    request_category: RequestCategory
    domain_confidence: float
    intent: Intent
    intent_confidence: float
    clarification_question: Optional[str]


_SAFE_FALLBACK = Classification(
    request_category=RequestCategory.OUT_OF_SCOPE,
    domain_confidence=0.0,
    intent=Intent.UNKNOWN,
    intent_confidence=0.0,
    clarification_question=None,
)

_CATEGORY_LOOKUP = {member.value: member for member in RequestCategory}
_INTENT_LOOKUP = {member.value: member for member in Intent}


def _coerce_category(raw: object) -> RequestCategory:
    return _CATEGORY_LOOKUP.get(str(raw), RequestCategory.OUT_OF_SCOPE)


def _coerce_intent(raw: object) -> Intent:
    return _INTENT_LOOKUP.get(str(raw), Intent.UNKNOWN)


def _coerce_confidence(raw: object) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def parse_llm_response(raw_text: str) -> Classification:
    """Parse the LLM's JSON response into a validated Classification.

    Total function: any malformed JSON or missing/invalid field degrades to
    the safe fallback (OUT_OF_SCOPE / UNKNOWN / zero confidence) instead of
    raising, so a single bad model response can never crash the graph.
    """
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return _SAFE_FALLBACK

    if not isinstance(payload, dict):
        return _SAFE_FALLBACK

    return Classification(
        request_category=_coerce_category(payload.get("request_category")),
        domain_confidence=_coerce_confidence(payload.get("domain_confidence")),
        intent=_coerce_intent(payload.get("intent")),
        intent_confidence=_coerce_confidence(payload.get("intent_confidence")),
        clarification_question=payload.get("clarification_question") or None,
    )
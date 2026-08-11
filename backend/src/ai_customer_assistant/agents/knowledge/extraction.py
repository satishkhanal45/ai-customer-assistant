"""
extraction.py — structured query extraction.

Converts a `RewrittenQuery` into a `StructuredQuery`, normalizing every
resolved slot into canonical ontology terms via `ontology.py`. Never
invents an entity type or attribute when a canonical term already
exists — see the module-level notes on graceful degradation below.

Same explicit-injection pattern as rewriting.py for the LLM callable.
`ontology.py`'s functions are pure and deterministic, so they're
imported directly rather than injected — no mocking needed to test
extraction logic against them.

Design decision — confidence is a value, not always an error:
`extract_query()` never raises for low confidence or for an
unresolvable entity_type/attribute guess from the LLM. A customer
asking "explain the RAG pipeline" will *correctly* produce a
StructuredQuery with every slot `None` and confidence 0.0 — that's the
expected signal telling `hybrid.py` to route to vector-only retrieval,
not a failure. Likewise, if the LLM's raw entity_type guess doesn't
canonicalize to anything in the ontology, that slot is dropped
(set to None, contributing 0.0 confidence) rather than raising and
aborting the whole graph — one ontology miss shouldn't take down
retrieval for what may still be a perfectly good vector-search
candidate. `LLMGenerationError` is reserved for genuine infrastructure
failures: the LLM call itself failing, or returning unparseable JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from . import ontology
from .config import KnowledgeAgentConfig
from .constants import PROMPT_TEMPLATE_EXTRACTION
from .exceptions import (
    LLMGenerationError,
    PromptTemplateNotFoundError,
    UnknownAttributeError,
    UnknownEntityTypeError,
)
from .types import CanonicalizationResult, QueryFilter, RewrittenQuery, StructuredQuery

LLMCompletion = Callable[[str], str]


def extract_query(
    rewritten: RewrittenQuery,
    *,
    config: KnowledgeAgentConfig,
    llm_complete: LLMCompletion,
) -> StructuredQuery:
    """Extract a StructuredQuery from a RewrittenQuery, normalizing every
    resolved slot against ontology.py.

    Raises PromptTemplateNotFoundError if prompts/extraction.md is
    missing. Raises LLMGenerationError if the LLM call fails or returns
    unparseable JSON. Never raises for an unresolvable entity_type or
    attribute guess, or for low confidence — those degrade gracefully
    into a StructuredQuery with the relevant slot(s) set to None."""
    template = _load_template(config.prompts_dir, PROMPT_TEMPLATE_EXTRACTION)
    prompt = _render_prompt(template, rewritten_text=rewritten.rewritten_text)
    raw_response = _invoke_llm(llm_complete, prompt)
    candidate = _parse_response(raw_response)
    return _canonicalize_candidate(candidate)


def is_confident(query: StructuredQuery, *, config: KnowledgeAgentConfig) -> bool:
    """Pure predicate for hybrid.py: does this StructuredQuery clear the
    configured extraction-confidence bar? Kept here (not duplicated in
    hybrid.py) since it's a property of extraction output, not of
    strategy selection itself."""
    return query.confidence >= config.extraction_confidence_threshold


# --------------------------------------------------------------------------
# Internals — prompt assembly
# --------------------------------------------------------------------------


def _load_template(prompts_dir: Path, template_name: str) -> str:
    """I/O boundary: read a prompt template file."""
    template_path = Path(prompts_dir) / template_name
    try:
        return template_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptTemplateNotFoundError(
            message=f"prompt template {template_name!r} not found in {prompts_dir}",
            template_name=template_name,
            prompts_dir=str(prompts_dir),
        ) from exc


def _format_ontology_reference() -> str:
    """Pure: render Domain -> Entity Types as a compact reference block,
    assembled from ontology.py's static tables rather than hardcoded
    here, so it always stays in sync with ontology.py."""
    return "\n".join(
        f"- {domain}: {', '.join(entity_types)}"
        for domain, entity_types in ontology.DOMAIN_ENTITY_TYPES.items()
    )


def _render_prompt(template: str, *, rewritten_text: str) -> str:
    """Pure: token substitution into the loaded template."""
    return template.replace(
        "{{ONTOLOGY_REFERENCE}}", _format_ontology_reference()
    ).replace(
        "{{REWRITTEN_QUERY}}", rewritten_text
    )


def _invoke_llm(llm_complete: LLMCompletion, prompt: str) -> str:
    try:
        return llm_complete(prompt)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any backend failure
        raise LLMGenerationError(
            message=f"structured-extraction LLM call failed: {exc}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """Pure: defensively strip a ```json ... ``` wrapper if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    inner_lines = lines[1:-1] if len(lines) >= 2 and lines[-1].startswith("```") else lines[1:]
    return "\n".join(inner_lines).strip()


# --------------------------------------------------------------------------
# Internals — response parsing (raw candidate, pre-canonicalization)
# --------------------------------------------------------------------------


class _RawCandidate:
    """Not a public type (see types.py for the package's public data
    layer) — a thin internal carrier for the LLM's raw, uncanonicalized
    guesses before they're resolved against ontology.py. Frozen to stay
    consistent with the rest of the codebase even though it never
    crosses a module boundary."""

    __slots__ = ("entity_type", "entity_label", "attribute", "relation_type", "filters", "confidence")

    def __init__(
        self,
        entity_type: Optional[str],
        entity_label: Optional[str],
        attribute: Optional[str],
        relation_type: Optional[str],
        filters: tuple[QueryFilter, ...],
        confidence: float,
    ) -> None:
        self.entity_type = entity_type
        self.entity_label = entity_label
        self.attribute = attribute
        self.relation_type = relation_type
        self.filters = filters
        self.confidence = confidence


def _non_empty_str_or_none(value: object) -> Optional[str]:
    """Pure: normalize a JSON value that should be `str | null` into
    `Optional[str]`, treating blank strings the same as null."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_filters(raw_filters: object) -> tuple[QueryFilter, ...]:
    if not isinstance(raw_filters, list):
        raise LLMGenerationError(
            message=f"structured-extraction response 'filters' must be a list: {raw_filters!r}"
        )
    if not all(
        isinstance(item, dict) and isinstance(item.get("field"), str) and isinstance(item.get("value"), str)
        for item in raw_filters
    ):
        raise LLMGenerationError(
            message=f"structured-extraction response 'filters' entries must be {{field, value}} strings: {raw_filters!r}"
        )
    return tuple(QueryFilter(field=item["field"], value=item["value"]) for item in raw_filters)


def _parse_confidence(raw_confidence: object) -> float:
    if not isinstance(raw_confidence, (int, float)) or isinstance(raw_confidence, bool):
        raise LLMGenerationError(
            message=f"structured-extraction response 'confidence' must be a number: {raw_confidence!r}"
        )
    if not 0.0 <= float(raw_confidence) <= 1.0:
        raise LLMGenerationError(
            message=f"structured-extraction response 'confidence' must be between 0.0 and 1.0: {raw_confidence!r}"
        )
    return float(raw_confidence)


def _parse_response(raw_response: str) -> _RawCandidate:
    """Raises LLMGenerationError (never returns a partial/None result)
    if the response isn't valid JSON or is missing the required shape."""
    candidate_text = _strip_code_fence(raw_response)
    try:
        parsed = json.loads(candidate_text)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(
            message=f"structured-extraction response was not valid JSON: {raw_response!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise LLMGenerationError(
            message=f"structured-extraction response must be a JSON object: {raw_response!r}"
        )

    return _RawCandidate(
        entity_type=_non_empty_str_or_none(parsed.get("entity_type")),
        entity_label=_non_empty_str_or_none(parsed.get("entity_label")),
        attribute=_non_empty_str_or_none(parsed.get("attribute")),
        relation_type=_non_empty_str_or_none(parsed.get("relation_type")),
        filters=_parse_filters(parsed.get("filters", [])),
        confidence=_parse_confidence(parsed.get("confidence", 0.0)),
    )


# --------------------------------------------------------------------------
# Internals — ontology canonicalization (graceful degradation on a miss)
# --------------------------------------------------------------------------


def _try_canonicalize_entity_type(candidate_text: Optional[str]) -> Optional[CanonicalizationResult]:
    if candidate_text is None:
        return None
    try:
        return ontology.canonicalize_entity_type(candidate_text)
    except UnknownEntityTypeError:
        return None


def _try_canonicalize_attribute(
    canonical_entity_type: Optional[str], candidate_text: Optional[str]
) -> Optional[CanonicalizationResult]:
    if canonical_entity_type is None or candidate_text is None:
        return None
    try:
        return ontology.canonicalize_attribute(canonical_entity_type, candidate_text)
    except (UnknownEntityTypeError, UnknownAttributeError):
        return None


def _resolve_filter(canonical_entity_type: Optional[str], raw_filter: QueryFilter) -> QueryFilter:
    """Best-effort: canonicalize a filter's field name against the
    resolved entity type's attributes when possible; otherwise pass the
    raw field through unchanged. Filters are advisory constraints, not
    EAV writes, so an ontology miss here is not an error."""
    resolved = _try_canonicalize_attribute(canonical_entity_type, raw_filter.field)
    return QueryFilter(field=resolved.canonical_term, value=raw_filter.value) if resolved else raw_filter


def _combined_confidence(*component_confidences: float) -> float:
    """Pure: mean of every applicable confidence signal. A missing slot
    contributes no term rather than a 0.0 penalty, so a query that never
    named an attribute isn't punished for lacking one."""
    return round(sum(component_confidences) / len(component_confidences), 2) if component_confidences else 0.0


def _canonicalize_candidate(candidate: _RawCandidate) -> StructuredQuery:
    entity_result = _try_canonicalize_entity_type(candidate.entity_type)
    canonical_entity_type = entity_result.canonical_term if entity_result else None

    attribute_result = _try_canonicalize_attribute(canonical_entity_type, candidate.attribute)
    canonical_attribute = attribute_result.canonical_term if attribute_result else None

    relation_result = ontology.canonicalize_relation_type(candidate.relation_type)
    canonical_relation_type = relation_result.canonical_term if relation_result else None

    resolved_filters = tuple(_resolve_filter(canonical_entity_type, f) for f in candidate.filters)

    # NOTE: gate on candidate.entity_type / candidate.attribute (did the LLM
    # attempt this slot at all), not on entity_result / attribute_result
    # (did canonicalization succeed) — an attempted-but-unresolved slot
    # must still contribute its 0.0 penalty to the combined confidence,
    # not be silently excluded from the average.
    component_confidences = (
        candidate.confidence,
        *((entity_result.confidence if entity_result else 0.0,) if candidate.entity_type is not None else ()),
        *((attribute_result.confidence if attribute_result else 0.0,) if candidate.attribute is not None else ()),
    )

    return StructuredQuery(
        entity_type=canonical_entity_type,
        entity_label=candidate.entity_label,
        attribute=canonical_attribute,
        relation_type=canonical_relation_type,
        filters=resolved_filters,
        confidence=_combined_confidence(*component_confidences),
    )
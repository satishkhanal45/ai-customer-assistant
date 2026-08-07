"""
llm.py — thin LLM generation layer.

`generate_response()` is deliberately the thinnest module in the
package: Prompt -> Configured Model -> GroundedResponse. No retrieval,
ranking, deduplication, or context-assembly logic belongs here — all of
that already happened upstream by the time a BuiltPrompt reaches this
module.

Groundedness is self-reported by the model in the same JSON response as
the answer itself (see prompts/answer.md's Output Format section),
rather than verified by a second pass here — that keeps this module a
single LLM call, matching "thin layer only." A fuller, independent
groundedness check (re-reading the answer against the context with a
separate judgment step) is the multi-agent system's Safety Agent's
job, external to the Knowledge Agent package (see the SAFE_GROUND node
in the architecture diagram) — not something llm.py re-implements.

`UngroundedResponseError` is declared in exceptions.py but deliberately
unused on this module's default path: an ungrounded answer
(`is_grounded=False`) is an expected, valid outcome the graph needs to
branch on (route to human escalation), not a failure this function
should raise for — the same "declared for extensibility, not every
module's default path" precedent as ContextBuildError in
context_builder.py.
"""

from __future__ import annotations

import json
from typing import Callable

from .exceptions import LLMGenerationError
from .types import BuiltContext, BuiltPrompt, ChunkProvenance, GroundedResponse

# An injected callable: (system_instructions, user_prompt) -> raw
# completion text. Two-argument (unlike rewriting.py/extraction.py's
# single-string LLMCompletion) because BuiltPrompt already keeps system
# instructions separate specifically so a provider with a dedicated
# system-prompt field (e.g. Anthropic's Messages API `system` param)
# can use it as such, rather than concatenating it back into one blob.
LLMCompletion = Callable[[str, str], str]


def generate_response(
    prompt: BuiltPrompt,
    *,
    context: BuiltContext,
    llm_complete: LLMCompletion,
) -> GroundedResponse:
    """Generate the final grounded answer.

    Raises LLMGenerationError if the LLM call fails or returns a
    response that cannot be parsed into a valid GroundedResponse. Never
    raises for an ungrounded-but-well-formed answer — see module
    docstring."""
    raw_response = _invoke_llm(llm_complete, prompt)
    parsed = _parse_response(raw_response)
    citations = _resolve_citations(parsed.citation_indices, context.cited_provenance)
    return GroundedResponse(answer_text=parsed.answer, is_grounded=parsed.is_grounded, citations=citations)


# --------------------------------------------------------------------------
# Internals — invocation
# --------------------------------------------------------------------------


def _invoke_llm(llm_complete: LLMCompletion, prompt: BuiltPrompt) -> str:
    try:
        return llm_complete(prompt.system_instructions, prompt.rendered_prompt)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any backend failure
        raise LLMGenerationError(message=f"answer-generation LLM call failed: {exc}") from exc


def _strip_code_fence(text: str) -> str:
    """Pure: defensively strip a ```json ... ``` wrapper if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    inner_lines = lines[1:-1] if len(lines) >= 2 and lines[-1].startswith("```") else lines[1:]
    return "\n".join(inner_lines).strip()


# --------------------------------------------------------------------------
# Internals — response parsing
# --------------------------------------------------------------------------


class _ParsedAnswer:
    """Internal carrier for the model's raw JSON response, not part of
    the package's public data layer (see types.py)."""

    __slots__ = ("answer", "is_grounded", "citation_indices")

    def __init__(self, answer: str, is_grounded: bool, citation_indices: tuple[int, ...]) -> None:
        self.answer = answer
        self.is_grounded = is_grounded
        self.citation_indices = citation_indices


def _parse_response(raw_response: str) -> _ParsedAnswer:
    candidate = _strip_code_fence(raw_response)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(
            message=f"answer-generation response was not valid JSON: {raw_response!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise LLMGenerationError(
            message=f"answer-generation response must be a JSON object: {raw_response!r}"
        )

    answer = parsed.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise LLMGenerationError(
            message=f"answer-generation response missing non-empty 'answer': {parsed!r}"
        )

    is_grounded = parsed.get("is_grounded")
    if not isinstance(is_grounded, bool):
        raise LLMGenerationError(
            message=f"answer-generation response 'is_grounded' must be a boolean: {parsed!r}"
        )

    citation_indices_raw = parsed.get("citation_indices", [])
    if not isinstance(citation_indices_raw, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in citation_indices_raw
    ):
        raise LLMGenerationError(
            message=f"answer-generation response 'citation_indices' must be a list of integers: {parsed!r}"
        )

    return _ParsedAnswer(
        answer=answer.strip(), is_grounded=is_grounded, citation_indices=tuple(citation_indices_raw)
    )


# --------------------------------------------------------------------------
# Internals — citation resolution
# --------------------------------------------------------------------------


def _resolve_citations(
    citation_indices: tuple[int, ...], cited_provenance: tuple[ChunkProvenance, ...]
) -> tuple[ChunkProvenance, ...]:
    """Pure: map the model's 1-based [n] citation markers back to the
    ChunkProvenance entries context_builder.py numbered them from.
    An index the model cited that's out of range (a model error, e.g.
    citing [5] when only 3 chunks existed) is silently dropped rather
    than raised — a malformed citation shouldn't fail the whole
    response when the answer text itself was otherwise valid."""
    return tuple(
        cited_provenance[index - 1] for index in citation_indices if 1 <= index <= len(cited_provenance)
    )
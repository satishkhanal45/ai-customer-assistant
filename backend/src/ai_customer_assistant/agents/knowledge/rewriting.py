"""
rewriting.py — conversational query rewriting.

Owns exactly one responsibility: turn the customer's latest message plus
conversation history into a self-contained, retrieval-friendly
`RewrittenQuery`. Never answers the question itself.

Follows the same explicit-dependency-injection pattern as
`chunk_embed/tokenizer.py` and `embedding.py`: the LLM callable is
injected by the caller (graph construction / nodes.py), not imported or
instantiated here. This keeps `rewrite_query()` unit-testable with a
simple fake — no mocks, no real network/model calls, matching the
`chunk_embed` test suite's approach.

The prompt template lives in `prompts/rewrite.md`, never hardcoded here,
per the project's prompt-management requirement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .config import KnowledgeAgentConfig
from .constants import PROMPT_TEMPLATE_REWRITE
from .exceptions import LLMGenerationError, PromptTemplateNotFoundError
from .types import RewrittenQuery

# A single injected callable: takes the fully-assembled prompt text,
# returns the raw LLM completion text. Whatever provider/model backs
# this (Anthropic, OpenAI, a fake for tests) is graph.py's concern, not
# this module's.
LLMCompletion = Callable[[str], str]

_NO_PRIOR_CONVERSATION: str = "(no prior conversation)"


def rewrite_query(
    raw_query: str,
    conversation_history: tuple[str, ...],
    *,
    config: KnowledgeAgentConfig,
    llm_complete: LLMCompletion,
) -> RewrittenQuery:
    """Rewrite `raw_query` into a self-contained, retrieval-friendly
    query, resolving conversational references against
    `conversation_history`.

    Raises PromptTemplateNotFoundError if prompts/rewrite.md is missing.
    Raises LLMGenerationError if the LLM call fails or returns a
    response that cannot be parsed into a valid RewrittenQuery."""
    template = _load_template(config.prompts_dir, PROMPT_TEMPLATE_REWRITE)
    prompt = _render_prompt(template, raw_query=raw_query, conversation_history=conversation_history)
    raw_response = _invoke_llm(llm_complete, prompt)
    return _parse_response(raw_response, original_text=raw_query)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _load_template(prompts_dir: Path, template_name: str) -> str:
    """I/O boundary: read a prompt template file. The only file-reading
    in this module."""
    template_path = Path(prompts_dir) / template_name
    try:
        return template_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptTemplateNotFoundError(
            message=f"prompt template {template_name!r} not found in {prompts_dir}",
            template_name=template_name,
            prompts_dir=str(prompts_dir),
        ) from exc


def _format_history(conversation_history: tuple[str, ...]) -> str:
    """Pure: numbered turn list, or a placeholder when history is empty."""
    if not conversation_history:
        return _NO_PRIOR_CONVERSATION
    return "\n".join(
        f"{turn_number}. {turn_text}"
        for turn_number, turn_text in enumerate(conversation_history, start=1)
    )


def _render_prompt(template: str, *, raw_query: str, conversation_history: tuple[str, ...]) -> str:
    """Pure: token substitution into the loaded template. Uses explicit
    placeholder tokens (not str.format) so the markdown template is free
    to contain literal `{`/`}` characters, e.g. inside the JSON example
    in prompts/rewrite.md, without collision."""
    return template.replace(
        "{{CONVERSATION_HISTORY}}", _format_history(conversation_history)
    ).replace(
        "{{RAW_QUERY}}", raw_query
    )


def _invoke_llm(llm_complete: LLMCompletion, prompt: str) -> str:
    try:
        return llm_complete(prompt)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any backend failure
        raise LLMGenerationError(
            message=f"query-rewrite LLM call failed: {exc}"
        ) from exc


def _strip_code_fence(text: str) -> str:
    """Pure: some models wrap JSON in ```json ... ``` despite
    instructions not to. Strip it defensively rather than failing."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    inner_lines = lines[1:-1] if len(lines) >= 2 and lines[-1].startswith("```") else lines[1:]
    return "\n".join(inner_lines).strip()


def _parse_response(raw_response: str, *, original_text: str) -> RewrittenQuery:
    """Pure: parse the LLM's JSON response into a RewrittenQuery.

    Raises LLMGenerationError (never returns None / a partial result) if
    the response isn't valid JSON or is missing the required shape."""
    candidate = _strip_code_fence(raw_response)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(
            message=f"query-rewrite response was not valid JSON: {raw_response!r}"
        ) from exc

    rewritten_text = parsed.get("rewritten_text") if isinstance(parsed, dict) else None
    if not isinstance(rewritten_text, str) or not rewritten_text.strip():
        raise LLMGenerationError(
            message=f"query-rewrite response missing non-empty 'rewritten_text': {parsed!r}"
        )

    resolved_references_raw = parsed.get("resolved_references", []) if isinstance(parsed, dict) else []
    if not isinstance(resolved_references_raw, list) or not all(
        isinstance(item, str) for item in resolved_references_raw
    ):
        raise LLMGenerationError(
            message=f"query-rewrite response 'resolved_references' must be a list of strings: {parsed!r}"
        )

    return RewrittenQuery(
        original_text=original_text,
        rewritten_text=rewritten_text.strip(),
        resolved_references=tuple(resolved_references_raw),
    )
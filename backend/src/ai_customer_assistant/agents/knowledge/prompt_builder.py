"""
prompt_builder.py — compose the final prompt sent to the LLM.

`build_prompt()` is a pure function (its only I/O is the same
`_load_template` file-read boundary used by rewriting.py/extraction.py):
it loads `prompts/answer.md`, splits it into system instructions and a
user-turn template at the `<!-- USER_PROMPT_TEMPLATE -->` marker, and
substitutes the conversation history, the two BuiltContext sections,
and the customer's question into the user-turn half.

No retrieval or ranking logic belongs here — by the time this module
runs, `context_builder.py` has already produced the final
`BuiltContext`; this module only renders it into prompt text.
"""

from __future__ import annotations

from functools import reduce
from pathlib import Path

from .config import KnowledgeAgentConfig
from .constants import PROMPT_TEMPLATE_ANSWER
from .exceptions import PromptBuildError, PromptTemplateNotFoundError
from .types import BuiltContext, BuiltPrompt

_TEMPLATE_DELIMITER = "<!-- USER_PROMPT_TEMPLATE -->"
_NO_PRIOR_CONVERSATION = "(no prior conversation)"


def build_prompt(
    context: BuiltContext,
    query: str,
    conversation_history: tuple[str, ...],
    *,
    config: KnowledgeAgentConfig,
) -> BuiltPrompt:
    """Compose the final BuiltPrompt from an already-built context.

    Raises PromptTemplateNotFoundError if prompts/answer.md is missing.
    Raises PromptBuildError if the template is missing the required
    `<!-- USER_PROMPT_TEMPLATE -->` delimiter separating system
    instructions from the user-turn template."""
    template = _load_template(config.prompts_dir, PROMPT_TEMPLATE_ANSWER)
    system_instructions, user_template = _split_template(template)
    rendered_prompt = _render_user_template(
        user_template, context=context, query=query, conversation_history=conversation_history
    )
    return BuiltPrompt(system_instructions=system_instructions.strip(), rendered_prompt=rendered_prompt)


# --------------------------------------------------------------------------
# Internals
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


def _split_template(template: str) -> tuple[str, str]:
    """Pure: split the loaded template at the delimiter into
    (system_instructions, user_template)."""
    if _TEMPLATE_DELIMITER not in template:
        raise PromptBuildError(
            message=f"answer.md is missing the required {_TEMPLATE_DELIMITER!r} delimiter"
        )
    system_instructions, user_template = template.split(_TEMPLATE_DELIMITER, maxsplit=1)
    return system_instructions, user_template


def _format_history(conversation_history: tuple[str, ...]) -> str:
    """Pure: numbered turn list, or a placeholder when history is empty."""
    if not conversation_history:
        return _NO_PRIOR_CONVERSATION
    return "\n".join(
        f"{turn_number}. {turn_text}"
        for turn_number, turn_text in enumerate(conversation_history, start=1)
    )


def _render_user_template(
    user_template: str,
    *,
    context: BuiltContext,
    query: str,
    conversation_history: tuple[str, ...],
) -> str:
    """Pure: token substitution into the user-turn half of the template."""
    substitutions = (
        ("{{CONVERSATION_HISTORY}}", _format_history(conversation_history)),
        ("{{STRUCTURED_FACTS}}", context.structured_section),
        ("{{RELEVANT_DOCUMENTATION}}", context.documentation_section),
        ("{{CUSTOMER_QUESTION}}", query),
    )
    return reduce(lambda text, token_value: text.replace(*token_value), substitutions, user_template).strip()
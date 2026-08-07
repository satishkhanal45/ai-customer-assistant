"""
Safe fallback response for ungrounded answers.

Static/templated, not dynamically generated (per project decision): the
project has no LLM client anywhere yet (confirmed — chat_service.py is
empty, and the Supervisor's own StubSupervisorLLMClient is the only
"LLM" currently wired in, used only for intent classification, not
free-text generation). Dynamic generation would require an LLM call
this project doesn't have the infrastructure for yet.

This is a plain, pure function — a template with the original query
interpolated in. Swapping this for real generation later only requires
changing generate_fallback_response()'s implementation; nothing else in
the Safety Agent depends on how the fallback text is produced.
"""

from __future__ import annotations

_FALLBACK_TEMPLATE = (
    'I wasn\'t able to confidently answer "{query}" from what I have on '
    "file. Would you like me to connect you with our support team?"
)


def generate_fallback_response(query: str) -> str:
    """
    Build the safe fallback message shown to the customer when an
    answer is ungrounded.

    Args:
        query: The customer's original question that could not be
               confidently answered.

    Returns:
        A templated fallback message with the query interpolated in.
        Leading/trailing whitespace on the query is stripped before
        interpolation.
    """
    return _FALLBACK_TEMPLATE.format(query=query.strip())

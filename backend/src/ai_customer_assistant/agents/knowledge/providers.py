"""agents/knowledge/providers.py — real Knowledge LLM provider callables.

Implements the Knowledge Agent's three injected LLM completions
(rewrite / extraction / answer), copying the provider pattern from
``supervisor/llm_client.py`` (Gemini / Groq clients + a deterministic
stub). The Knowledge graph only ever sees three plain callables:

    rewrite_llm_complete(prompt: str) -> str          (agent/knowledge/rewriting.py)
    extraction_llm_complete(prompt: str) -> str       (agent/knowledge/extraction.py)
    answer_llm_complete(system, user) -> str          (agent/knowledge/llm.py)

`KnowledgeProvider` is the protocol all real backends satisfy, and
`llm_completions(provider)` turns a provider instance into the three
callables in the exact shape `build_knowledge_agent_graph` expects.

Provider selection (`build_knowledge_provider`) mirrors the Supervisor's
``build_llm_client`` convention (agents_integration_plan_new.md §4.6):
an explicit ``provider`` argument wins; otherwise the config's
``KnowledgeAgentConfig.llm_provider``; otherwise the deterministic stub
used in dev/tests. A configured real provider (anthropic/groq/gemini)
whose API key is absent from the environment also falls back to the stub,
so the service keeps running without credentials rather than failing at
startup — matching the Supervisor resolver's GROQ_API_KEY behaviour.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional, Protocol, TypeAlias

import groq

from .config import KnowledgeAgentConfig


class KnowledgeProvider(Protocol):
    """Shape every Knowledge LLM backend satisfies."""

    def rewrite_complete(self, prompt: str) -> str: ...
    def extraction_complete(self, prompt: str) -> str: ...
    def answer_complete(self, system_instructions: str, user_prompt: str) -> str: ...


LLMCompletions: TypeAlias = dict[str, Callable[[str], str] | Callable[[str, str], str]]


# ---------------------------------------------------------------------------
# Deterministic stub — used in dev/tests when no provider credential exists.
# ---------------------------------------------------------------------------


class StubKnowledgeProvider:
    """Deterministic stand-in. Returns parseable JSON in the exact shape
    each stage's parser expects, so a full Knowledge turn is runnable end-
    to-end without any network/model. Answers are deliberately generic."""

    def rewrite_complete(self, prompt: str) -> str:
        return json.dumps(
            {
                "rewritten_text": "customer's support question",
                "resolved_references": [],
            }
        )

    def extraction_complete(self, prompt: str) -> str:
        return json.dumps(
            {
                "entity_type": None,
                "entity_label": None,
                "attribute": None,
                "relation_type": None,
                "filters": [],
                "confidence": 0.0,
            }
        )

    def answer_complete(self, system_instructions: str, user_prompt: str) -> str:
        return json.dumps(
            {
                "answer": "I don't have an answer to that question yet.",
                "is_grounded": False,
                "citation_indices": [],
            }
        )


# ---------------------------------------------------------------------------
# Anthropic backend — Messages API with a dedicated ``system`` field.
# ---------------------------------------------------------------------------


class AnthropicKnowledgeProvider:
    """Anthropic Messages-API backend (``anthropic`` SDK, already a
    project dependency). The answer completion separates ``system`` (the
    ``system`` API field) from the user prompt; the rewrite/extraction
    stages are single user turns on the rewrite model."""

    _DEFAULT_MODEL = "claude-sonnet-5"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        rewrite_model: str = _DEFAULT_MODEL,
        timeout: float = 30.0,
        max_tokens: int = 1024,
    ) -> None:
        import anthropic

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError("ANTHROPIC_API_KEY not set.")

        self.client = anthropic.Anthropic(api_key=resolved_key)
        self.model = model
        self.rewrite_model = rewrite_model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def rewrite_complete(self, prompt: str) -> str:
        return self._single_turn(prompt, model=self.rewrite_model)

    def extraction_complete(self, prompt: str) -> str:
        return self._single_turn(prompt, model=self.rewrite_model)

    def answer_complete(self, system_instructions: str, user_prompt: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,
            system=system_instructions,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _extract_text(response.content)

    def _single_turn(self, prompt: str, *, model: str) -> str:
        response = self.client.messages.create(
            model=model,
            max_tokens=self.max_tokens,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return _extract_text(response.content)


# ---------------------------------------------------------------------------
# Groq backend (openai-compatible chat completions).
# ---------------------------------------------------------------------------


class GroqKnowledgeProvider:
    """Groq-backed provider (``openai/gpt-oss-120b`` default), mirroring
    the Supervisor's Groq client."""

    _DEFAULT_MODEL = "openai/gpt-oss-120b"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        rewrite_model: str = _DEFAULT_MODEL,
        timeout: float = 15.0,
    ) -> None:
        resolved_key = api_key or os.environ.get("GROQ_API_KEY")
        if not resolved_key:
            raise ValueError("GROQ_API_KEY not set.")

        self.client = groq.Groq(api_key=resolved_key)
        self.model = model
        self.rewrite_model = rewrite_model
        self.timeout = timeout

    def rewrite_complete(self, prompt: str) -> str:
        return self._complete([{"role": "user", "content": prompt}], model=self.rewrite_model)

    def extraction_complete(self, prompt: str) -> str:
        return self._complete([{"role": "user", "content": prompt}], model=self.rewrite_model)

    def answer_complete(self, system_instructions: str, user_prompt: str) -> str:
        return self._complete(
            [
                {"role": "system", "content": system_instructions},
                {"role": "user", "content": user_prompt},
            ],
            model=self.model,
        )

    def _complete(self, messages: list[dict], *, model: str) -> str:
        last_error: Optional[groq.APIError] = None
        for _ in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0,
                    timeout=self.timeout,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content or "{}"
            except groq.APIError as exc:
                last_error = exc

        raise last_error


# ---------------------------------------------------------------------------
# Gemini backend (REST generateContent, mirroring Supervisor's Gemini client)
# ---------------------------------------------------------------------------


class GeminiKnowledgeProvider:
    """Gemini-backed provider, mirroring the Supervisor's Gemini client:
    REST ``generateContent`` against ``generativelanguage.googleapis.com``.
    Rewrite/extraction use a single system-less user turn; the answer uses
    the separate ``system_instruction`` field the API supports."""

    _ENDPOINT_TEMPLATE = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )
    _DEFAULT_MODEL = "gemini-2.0-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        rewrite_model: str = _DEFAULT_MODEL,
        timeout: float = 15.0,
    ) -> None:
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError("GEMINI_API_KEY not set.")

        self.api_key = resolved_key
        self.model = model
        self.rewrite_model = rewrite_model
        self.timeout = timeout

    def rewrite_complete(self, prompt: str) -> str:
        return self._generate(
            payload={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            model=self.rewrite_model,
        )

    def extraction_complete(self, prompt: str) -> str:
        return self._generate(
            payload={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            model=self.rewrite_model,
        )

    def answer_complete(self, system_instructions: str, user_prompt: str) -> str:
        return self._generate(
            payload={
                "system_instruction": {"parts": [{"text": system_instructions}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            },
            model=self.model,
        )

    def _generate(self, payload: dict, *, model: str) -> str:
        import requests

        response = requests.post(
            self._ENDPOINT_TEMPLATE.format(model=model),
            params={"key": self.api_key},
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini Knowledge call failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return "{}"
        return "".join(part.get("text", "") for part in parts)


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

_PROVIDER_FACTORIES: dict[str, Callable[..., KnowledgeProvider]] = {
    "stub": StubKnowledgeProvider,
    "anthropic": AnthropicKnowledgeProvider,
    "groq": GroqKnowledgeProvider,
    "gemini": GeminiKnowledgeProvider,
}

_ENV_VAR_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_DEFAULT_MODEL = {
    "anthropic": AnthropicKnowledgeProvider._DEFAULT_MODEL,
    "groq": GroqKnowledgeProvider._DEFAULT_MODEL,
    "gemini": GeminiKnowledgeProvider._DEFAULT_MODEL,
}


def _models_from_config(config: Optional[KnowledgeAgentConfig]) -> tuple[str, str]:
    """Return ``(answer_model, rewrite_model)``, honouring the config's
    split model fields with the generic stub name as the fallback."""
    if config is None:
        return "unknown-model", "unknown-model"
    answer_model = getattr(config, "llm_model_name", "unknown-model") or "unknown-model"
    rewrite_model = getattr(config, "rewrite_model_name", "unknown-model") or "unknown-model"
    return answer_model, rewrite_model


def build_knowledge_provider(
    config: Optional[KnowledgeAgentConfig] = None,
    provider: Optional[str] = None,
) -> KnowledgeProvider:
    """Resolve a Knowledge LLM provider.

    Selection order, mirroring the Supervisor's ``build_llm_client``:
    explicit ``provider`` argument > ``config.llm_provider`` (if that
    credential exists) > first available credential (groq, then gemini) >
    stub. A real provider whose API key is missing from the environment
    also degrades to the deterministic stub (the service must always
    start); as with the Supervisor, when auto-selecting a provider other
    than the one the config named, the provider's own default model is
    used so a config tuned for one vendor never leaks its model names
    into another vendor's API calls.
    """
    resolved_provider = provider or _resolved_provider_from_config(config)

    if resolved_provider not in _PROVIDER_FACTORIES:
        available = sorted(_PROVIDER_FACTORIES)
        raise ValueError(f"Unknown provider {resolved_provider!r}. Available: {available}")

    if resolved_provider == "stub" or not _has_credentials(resolved_provider):
        return StubKnowledgeProvider()

    factory = _PROVIDER_FACTORIES[resolved_provider]
    configured_provider = getattr(config, "llm_provider", None) if config else None
    if configured_provider == resolved_provider:
        answer_model, rewrite_model = _models_from_config(config)
        if answer_model == "unknown-model" or rewrite_model == "unknown-model":
            answer_model = rewrite_model = _DEFAULT_MODEL[resolved_provider]
    else:
        answer_model = rewrite_model = _DEFAULT_MODEL[resolved_provider]

    return factory(model=answer_model, rewrite_model=rewrite_model)


def _resolved_provider_from_config(config: Optional[KnowledgeAgentConfig]) -> str:
    """Resolve a provider: explicit argument > config's ``llm_provider``
    (if that credential exists) > first available key from the shared
    provider credentials (matching the Supervisor's ``build_llm_client``
    auto-selection) > stub."""
    if config is None:
        return "stub"
    configured = getattr(config, "llm_provider", "stub")
    if configured in _PROVIDER_FACTORIES and _has_credentials(configured):
        return configured
    for candidate in ("groq", "gemini"):
        if _has_credentials(candidate):
            return candidate
    return "stub"


def _has_credentials(provider: str) -> bool:
    env_var = _ENV_VAR_BY_PROVIDER.get(provider)
    return bool(env_var and os.environ.get(env_var))


def llm_completions(provider: KnowledgeProvider) -> LLMCompletions:
    """Return the three callables in the exact shape
    ``build_knowledge_agent_graph`` expects to inject into its nodes."""
    return {
        "rewrite_llm_complete": provider.rewrite_complete,
        "extraction_llm_complete": provider.extraction_complete,
        "answer_llm_complete": provider.answer_complete,
    }


def _extract_text(content) -> str:
    """Join Anthropic's content blocks (avoiding any rebuilding if a fake
    returns a plain string, e.g. in tests)."""
    if isinstance(content, str):
        return content
    try:
        return "".join(
            block.text for block in content if getattr(block, "type", "") == "text"
        )
    except (KeyError, IndexError, TypeError):
        return "{}"
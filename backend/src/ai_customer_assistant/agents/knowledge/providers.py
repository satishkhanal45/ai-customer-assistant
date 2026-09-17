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
import time
from typing import Callable, Optional, Protocol, TypeAlias

import groq

from rate_limit_signal import note_rate_limited
from agents.supervisor.routing import is_rate_limited
from timeouts import (
    LLM_ANSWER_RETRY_BUDGET_S,
    LLM_ANSWER_TIMEOUT_S,
    LLM_SHORT_RETRY_BUDGET_S,
    LLM_SHORT_TIMEOUT_S,
    sleep_within_budget,
)

import llm_credentials

from .config import KnowledgeAgentConfig

# Transient-call budget for the Groq backend: connection blips and 429 rate
# limits are retried with exponential backoff (honouring Groq's "try again in
# XmYs" hint when present) before the error is surfaced to the graph.
#
# The budget is wall-clock, not a sleep clamp. Clamping each individual sleep
# to 300s (what this used to do) still let one call sit here for minutes,
# long after the 45s Knowledge node timeout had already abandoned it — the
# retries were burning quota for an answer nobody would receive. Now the loop
# stops as soon as the *next* attempt could not finish inside what is left of
# the stage's retry budget, and surfaces the last error instead.
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 1.5


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
        timeout: float = LLM_SHORT_TIMEOUT_S,
        answer_timeout: float = LLM_ANSWER_TIMEOUT_S,
        max_tokens: int = 1024,
    ) -> None:
        import anthropic

        resolved_key = api_key or llm_credentials.api_key_for("anthropic")
        if not resolved_key:
            raise ValueError("ANTHROPIC_API_KEY not set.")

        self.client = anthropic.Anthropic(api_key=resolved_key)
        self.model = model
        self.rewrite_model = rewrite_model
        self.timeout = timeout
        self.answer_timeout = answer_timeout
        self.max_tokens = max_tokens

    def rewrite_complete(self, prompt: str) -> str:
        return self._single_turn(prompt, model=self.rewrite_model)

    def extraction_complete(self, prompt: str) -> str:
        return self._single_turn(prompt, model=self.rewrite_model)

    def answer_complete(self, system_instructions: str, user_prompt: str) -> str:
        # `timeout` was stored here and never passed to the SDK, so these
        # calls had no deadline at all — the same defect the Supervisor's
        # Groq client carried. Generation gets the answer-stage budget.
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,
            timeout=self.answer_timeout,
            system=system_instructions,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _extract_text(response.content)

    def _single_turn(self, prompt: str, *, model: str) -> str:
        response = self.client.messages.create(
            model=model,
            max_tokens=self.max_tokens,
            temperature=0,
            timeout=self.timeout,
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

    # Class-level defaults so the retry loop stays correct for an instance
    # built without __init__ (the test fakes construct via __new__), and so
    # the ladder in timeouts.py is the single place these are declared.
    #
    # Two pairs, not one: rewrite and extraction emit a few tokens of JSON,
    # while answer generation emits paragraphs and legitimately takes an
    # order of magnitude longer. A single shared timeout was cutting off
    # healthy generations as failures — see timeouts.py for the measurements.
    timeout: float = LLM_SHORT_TIMEOUT_S
    retry_budget: float = LLM_SHORT_RETRY_BUDGET_S
    answer_timeout: float = LLM_ANSWER_TIMEOUT_S
    answer_retry_budget: float = LLM_ANSWER_RETRY_BUDGET_S

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        rewrite_model: str = _DEFAULT_MODEL,
        timeout: float = LLM_SHORT_TIMEOUT_S,
        retry_budget: float = LLM_SHORT_RETRY_BUDGET_S,
        answer_timeout: float = LLM_ANSWER_TIMEOUT_S,
        answer_retry_budget: float = LLM_ANSWER_RETRY_BUDGET_S,
    ) -> None:
        resolved_key = api_key or llm_credentials.api_key_for("groq")
        if not resolved_key:
            raise ValueError("GROQ_API_KEY not set.")

        self.client = groq.Groq(api_key=resolved_key)
        self.model = model
        self.rewrite_model = rewrite_model
        self.timeout = timeout
        self.retry_budget = retry_budget
        self.answer_timeout = answer_timeout
        self.answer_retry_budget = answer_retry_budget

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
            timeout=self.answer_timeout,
            retry_budget=self.answer_retry_budget,
        )

    def _complete(
        self,
        messages: list[dict],
        *,
        model: str,
        timeout: Optional[float] = None,
        retry_budget: Optional[float] = None,
    ) -> str:
        """One logical completion, retried within a wall-clock budget.

        ``timeout`` / ``retry_budget`` default to this provider's *short*
        stage values; ``answer_complete`` passes its own, larger pair.
        """
        timeout = self.timeout if timeout is None else timeout
        retry_budget = self.retry_budget if retry_budget is None else retry_budget

        deadline = time.monotonic() + retry_budget
        last_error: Optional[Exception] = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0,
                    timeout=timeout,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content or "{}"
            except Exception as exc:  # noqa: BLE001 - APIError + wrapped connection errors
                last_error = exc
                # Leave a note the node can read if the turn's clock runs out
                # before this loop does: a timeout on its own cannot say
                # whether it was throttled or merely slow, and telling a
                # throttled customer "something went wrong on my end" sends
                # them looking for a bug that is not there.
                if is_rate_limited(exc):
                    note_rate_limited()
            if attempt < _RETRY_ATTEMPTS - 1:
                cooldown = _cooldown_seconds(str(last_error)) or _RETRY_BASE_DELAY
                if not sleep_within_budget(cooldown * (attempt + 1), deadline, timeout):
                    break
        assert last_error is not None
        raise last_error


def _cooldown_seconds(message: str) -> float | None:
    """Parse Groq's 'Please try again in 6m33.552s.' hint into seconds."""
    for token in message.replace(",", " ").split():
        stripped = token.rstrip(".")
        if "m" in stripped and stripped.endswith("s"):
            minutes_part, seconds_part = stripped[:-1].split("m", 1)
            try:
                return float(minutes_part) * 60 + float(seconds_part)
            except ValueError:
                continue
        try:
            if stripped.endswith("m"):
                return float(stripped[:-1]) * 60
            if stripped.endswith("s"):
                return float(stripped[:-1])
        except ValueError:
            continue
    return None


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
        timeout: float = LLM_SHORT_TIMEOUT_S,
        answer_timeout: float = LLM_ANSWER_TIMEOUT_S,
    ) -> None:
        resolved_key = api_key or llm_credentials.api_key_for("gemini")
        if not resolved_key:
            raise ValueError("GEMINI_API_KEY not set.")

        self.api_key = resolved_key
        self.model = model
        self.rewrite_model = rewrite_model
        self.timeout = timeout
        self.answer_timeout = answer_timeout

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
            timeout=self.answer_timeout,
        )

    def _generate(self, payload: dict, *, model: str, timeout: Optional[float] = None) -> str:
        import requests

        response = requests.post(
            self._ENDPOINT_TEMPLATE.format(model=model),
            params={"key": self.api_key},
            json=payload,
            timeout=self.timeout if timeout is None else timeout,
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
    # The administrator's chosen default is tried before the hard-coded
    # order, so setting it on the Admin page actually decides something.
    # The trailing sweep over every registered provider is what makes
    # "paste a key for any supported vendor and it works" true -- without
    # it, a deployment holding only a key this tuple does not name falls
    # through to the stub, which answers plausibly and cites nothing.
    for candidate in (
        llm_credentials.default_provider(),
        "groq",
        "gemini",
        *(p.name for p in llm_credentials.PROVIDERS),
    ):
        if candidate in _PROVIDER_FACTORIES and _has_credentials(candidate):
            return candidate
    return "stub"


def _has_credentials(provider: str) -> bool:
    """Whether this provider has a key at all -- saved or in the environment.

    `llm_credentials.api_key_for` checks the admin-saved key first and the
    provider's environment variable second, so a deployment that never uses
    the Admin page behaves exactly as it did when this read `os.environ`
    directly.
    """
    return bool(llm_credentials.api_key_for(provider))


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
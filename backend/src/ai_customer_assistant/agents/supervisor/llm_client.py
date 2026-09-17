from __future__ import annotations

import json
import os

import llm_credentials
import time
from typing import Callable, Optional, Protocol

import groq

from rate_limit_signal import note_rate_limited
from .routing import is_rate_limited
from groq import Groq

from timeouts import CLASSIFY_BUDGET_S, LLM_SHORT_TIMEOUT_S, sleep_within_budget

from .schema import ConversationTurn


class SupervisorLLMClient(Protocol):
    """Anything with this shape can back the Supervisor's classification step."""

    def classify(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[ConversationTurn],
    ) -> str:
        ...


class StubSupervisorLLMClient:
    """Deterministic stand-in used until a real provider is configured."""

    def classify(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[ConversationTurn],
    ) -> str:
        return json.dumps(
            {
                "request_category": "DOMAIN_REQUEST",
                "domain_confidence": 0.0,
                "intent": "UNKNOWN",
                "intent_confidence": 0.0,
                "clarification_question": None,
            }
        )


def _to_gemini_role(role: str) -> str:
    return "model" if role == "assistant" else "user"


def _extract_gemini_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)
    except (KeyError, IndexError, TypeError):
        return "{}"


_GEMINI_STATUS_MESSAGES = {
    400: (
        "Gemini rejected the request (400 Bad Request) — usually a malformed "
        "payload or an unsupported model name."
    ),
    403: (
        "Gemini rejected the API key (403 Forbidden)."
    ),
    404: (
        "Gemini returned 404 Not Found — the model name is likely wrong."
    ),
    429: (
        "Gemini's free-tier rate limit was hit (429 Too Many Requests)."
    ),
}


def _gemini_http_error(response, original: Exception) -> Exception:
    message = _GEMINI_STATUS_MESSAGES.get(response.status_code)
    return RuntimeError(message) if message else original


def _extract_anthropic_text(content) -> str:
    """Join the text blocks of a Messages-API response.

    Returns ``""`` rather than raising on an unexpected shape, so the
    caller's prefill still produces parseable-or-fallback JSON instead of
    an exception on the first LLM call of a turn.
    """
    try:
        return "".join(
            block.text for block in content if getattr(block, "type", None) == "text"
        )
    except TypeError:
        return ""


def _anthropic_messages(
    user_message: str, conversation_history: list[ConversationTurn]
) -> list[dict]:
    """Pure: history plus the new message, as an Anthropic message list.

    Anthropic requires the first message to be ``user`` and rejects two
    consecutive messages with the same role. Neither is guaranteed here:
    ``node._bounded_history`` keeps a *tail* of the conversation, so it can
    cut a user/assistant pair in half and hand us a history that opens on
    an assistant turn. Groq and Gemini tolerate that shape, which is why
    this normalisation lives at this boundary rather than in the caller.

    Leading assistant turns are dropped (they answer a question that is no
    longer in the window) and same-role runs are merged.
    """
    turns = list(conversation_history)
    while turns and turns[0].role != "user":
        turns.pop(0)

    messages: list[dict] = []
    for turn in turns:
        if messages and messages[-1]["role"] == turn.role:
            messages[-1]["content"] = f"{messages[-1]['content']}\n\n{turn.content}"
        else:
            messages.append({"role": turn.role, "content": turn.content})

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = f"{messages[-1]['content']}\n\n{user_message}"
    else:
        messages.append({"role": "user", "content": user_message})
    return messages


class AnthropicSupervisorLLMClient:
    """Anthropic-backed classification, mirroring the Groq client.

    Two API differences are handled here rather than pushed onto callers:

    * **There is no ``response_format={"type": "json_object"}``.** The
      Messages API gets JSON by *prefilling* the assistant turn with an
      opening brace, which constrains the very first token; the brace is
      prepended back onto the reply. ``parse_llm_response`` already
      degrades any malformed payload to the safe fallback, so a model that
      ignores the prefill costs one classification, not the turn.
    * **The system prompt is a request field, not a message**, which is
      also how ``AnthropicKnowledgeProvider`` sends it.

    ``max_tokens`` is required by this API and classification emits one
    small JSON object, so the cap is generous rather than tuned.
    """

    _DEFAULT_MODEL = "claude-sonnet-5"
    _JSON_PREFILL = "{"

    # Class-level defaults, so an instance built without __init__ (the test
    # fakes construct via __new__) still has a bounded retry loop -- the
    # same reason the Groq client declares its own.
    timeout: float = LLM_SHORT_TIMEOUT_S
    retry_budget: float = CLASSIFY_BUDGET_S
    max_tokens: int = 512

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        timeout: float = LLM_SHORT_TIMEOUT_S,
        retry_budget: float = CLASSIFY_BUDGET_S,
        max_tokens: int = 512,
    ) -> None:
        import anthropic

        resolved_key = api_key or llm_credentials.api_key_for("anthropic")
        if not resolved_key:
            raise ValueError("ANTHROPIC_API_KEY not set.")

        self.client = anthropic.Anthropic(api_key=resolved_key)
        self.model = model
        self.timeout = timeout
        self.retry_budget = retry_budget
        self.max_tokens = max_tokens

    def classify(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[ConversationTurn],
    ) -> str:
        messages = [
            *_anthropic_messages(user_message, conversation_history),
            {"role": "assistant", "content": self._JSON_PREFILL},
        ]

        # Bounded by wall clock, not attempt count -- see the Groq client
        # below for the F1 defect that shape exists to prevent.
        deadline = time.monotonic() + self.retry_budget
        last_error: Optional[Exception] = None
        attempts = 3
        for attempt in range(attempts):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=0,
                    timeout=self.timeout,
                    system=system_prompt,
                    messages=messages,
                )
                return self._JSON_PREFILL + _extract_anthropic_text(response.content)
            except Exception as exc:  # noqa: BLE001 - any SDK/transport error
                # Matched on shape rather than on an imported exception
                # class, exactly as `routing.is_rate_limited` documents, so
                # this stays correct across SDK versions.
                if is_rate_limited(exc):
                    note_rate_limited()
                last_error = exc
            if attempt < attempts - 1:
                if not sleep_within_budget(0.5 * (attempt + 1), deadline, self.timeout):
                    break

        assert last_error is not None
        raise last_error


class GeminiSupervisorLLMClient:
    _ENDPOINT_TEMPLATE = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.0-flash",
        timeout: float = LLM_SHORT_TIMEOUT_S,
    ) -> None:
        resolved_key = api_key or llm_credentials.api_key_for("gemini")
        if not resolved_key:
            raise ValueError("GEMINI_API_KEY not set.")

        self.api_key = resolved_key
        self.model = model
        self.timeout = timeout

    def classify(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[ConversationTurn],
    ) -> str:
        import requests

        contents = [
            *(
                {
                    "role": _to_gemini_role(turn.role),
                    "parts": [{"text": turn.content}],
                }
                for turn in conversation_history
            ),
            {
                "role": "user",
                "parts": [{"text": user_message}],
            },
        ]

        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": contents,
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0,
            },
        }

        response = requests.post(
            self._ENDPOINT_TEMPLATE.format(model=self.model),
            params={"key": self.api_key},
            json=payload,
            timeout=self.timeout,
        )

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise _gemini_http_error(response, exc) from exc

        return _extract_gemini_text(response.json())


class GroqSupervisorLLMClient:
    """Groq-backed implementation."""

    # Class-level defaults, so an instance built without __init__ (the test
    # fakes construct via __new__) still has a bounded retry loop.
    timeout: float = LLM_SHORT_TIMEOUT_S
    retry_budget: float = CLASSIFY_BUDGET_S

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "openai/gpt-oss-120b",
        timeout: float = LLM_SHORT_TIMEOUT_S,
        retry_budget: float = CLASSIFY_BUDGET_S,
    ) -> None:
        resolved_key = api_key or llm_credentials.api_key_for("groq")
        if not resolved_key:
            raise ValueError("GROQ_API_KEY not set.")

        self.client = Groq(api_key=resolved_key)
        self.model = model
        self.timeout = timeout
        self.retry_budget = retry_budget

    def classify(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[ConversationTurn],
    ) -> str:

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            *(
                {
                    "role": turn.role,
                    "content": turn.content,
                }
                for turn in conversation_history
            ),
            {
                "role": "user",
                "content": user_message,
            },
        ]

        # Two separate bugs lived in this loop.
        #
        # `timeout=self.timeout` was stored but never passed to the API call,
        # so classification had no socket deadline at all: a stalled connection
        # held the chat turn open indefinitely while the browser gave up at 60s.
        #
        # And the loop was bounded by attempt count only, so its worst case was
        # 3 x 10s of socket timeout plus backoff — about 31s — for a call that
        # measures 1.1s. That was the unbounded term in `classify + knowledge
        # node + checkpointing`, and it is why a turn could reach ~76s while
        # every individual rung honoured its own budget (F1). It is now bounded
        # by wall clock, like the Knowledge provider's loop.
        deadline = time.monotonic() + self.retry_budget
        last_error: Optional[groq.APIError] = None
        attempts = 3
        for attempt in range(attempts):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0,
                    timeout=self.timeout,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content or "{}"
            except groq.APIError as exc:
                # Classification is the first LLM call of a turn, so on a
                # tight per-minute budget it is usually the first throttled.
                if is_rate_limited(exc):
                    note_rate_limited()
                last_error = exc
            # No sleep after the final attempt — it delayed the error by a
            # second and a half without buying another try. And no sleep at
            # all once the remaining budget could not hold the retry it
            # precedes: waiting for a call nobody will wait for is pure cost.
            if attempt < attempts - 1:
                if not sleep_within_budget(0.5 * (attempt + 1), deadline, self.timeout):
                    break

        raise last_error


_PROVIDER_FACTORIES: dict[str, Callable[[], SupervisorLLMClient]] = {
    "stub": StubSupervisorLLMClient,
    "anthropic": AnthropicSupervisorLLMClient,
    "gemini": GeminiSupervisorLLMClient,
    "groq": GroqSupervisorLLMClient,
}


def _provider_candidates() -> tuple[str, ...]:
    """Pure: the resolution order for an unspecified provider.

    The administrator's default first, then Groq (what this deployment
    runs on and what the timeout ladder was measured against), then every
    other provider that has a key.

    That last clause is the important one. Without it a deployment holding
    *only* a non-Groq key fell through to the stub classifier -- which
    answers every question with ``OUT_OF_SCOPE``/``UNKNOWN`` and raises
    nothing, so the symptom was a system that started cleanly and then
    declined every question. The Knowledge Agent has always fallen back to
    "first available credential"; this makes the Supervisor agree with it.
    """
    return (
        llm_credentials.default_provider(),
        "groq",
        *(p.name for p in llm_credentials.PROVIDERS),
    )


def build_llm_client(provider: Optional[str] = None) -> SupervisorLLMClient:
    resolved_provider = provider
    if resolved_provider is None:
        for candidate in _provider_candidates():
            if candidate in _PROVIDER_FACTORIES and llm_credentials.api_key_for(candidate):
                resolved_provider = candidate
                break
    if resolved_provider is None:
        resolved_provider = "stub"

    try:
        factory = _PROVIDER_FACTORIES[resolved_provider]
    except KeyError as exc:
        available = sorted(_PROVIDER_FACTORIES)
        raise ValueError(
            f"Unknown provider {resolved_provider!r}. Available: {available}"
        ) from exc

    return factory()
from __future__ import annotations

import json
import os

import llm_credentials
import time
from typing import Callable, Optional, Protocol

import groq
from groq import Groq

from rate_limit_signal import note_rate_limited

from .routing import is_rate_limited
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
                # tight per-minute budget it is usually the first to be
                # throttled. Recording it here is what lets the turn's
                # timeout say "busy" instead of "something went wrong".
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
    "gemini": GeminiSupervisorLLMClient,
    "groq": GroqSupervisorLLMClient,
}


def build_llm_client(provider: Optional[str] = None) -> SupervisorLLMClient:
    # The administrator's default first, then Groq, then the stub -- so the
    # Admin page's choice decides which provider classifies a turn, and a
    # deployment with no key still starts.
    resolved_provider = provider
    if resolved_provider is None:
        for candidate in (llm_credentials.default_provider(), "groq"):
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
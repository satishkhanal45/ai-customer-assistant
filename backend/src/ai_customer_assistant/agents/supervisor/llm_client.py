from __future__ import annotations

import json
import os
from typing import Callable, Optional, Protocol

from groq import Groq

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
        timeout: float = 15.0,
    ) -> None:
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
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
                    "role": _to_gemini_role(turn["role"]),
                    "parts": [{"text": turn["content"]}],
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

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "openai/gpt-oss-120b",
        timeout: float = 15.0,
    ) -> None:
        resolved_key = api_key or os.environ.get("GROQ_API_KEY")
        if not resolved_key:
            raise ValueError("GROQ_API_KEY not set.")

        self.client = Groq(api_key=resolved_key)
        self.model = model
        self.timeout = timeout

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
                    "role": turn["role"],
                    "content": turn["content"],
                }
                for turn in conversation_history
            ),
            {
                "role": "user",
                "content": user_message,
            },
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )

        return response.choices[0].message.content or "{}"


_PROVIDER_FACTORIES: dict[str, Callable[[], SupervisorLLMClient]] = {
    "stub": StubSupervisorLLMClient,
    "gemini": GeminiSupervisorLLMClient,
    "groq": GroqSupervisorLLMClient,
}


def build_llm_client(provider: Optional[str] = None) -> SupervisorLLMClient:
    resolved_provider = (
        provider
        if provider is not None
        else "groq" if os.environ.get("GROQ_API_KEY")
        else "stub"
    )

    try:
        factory = _PROVIDER_FACTORIES[resolved_provider]
    except KeyError as exc:
        available = sorted(_PROVIDER_FACTORIES)
        raise ValueError(
            f"Unknown provider {resolved_provider!r}. Available: {available}"
        ) from exc

    return factory()
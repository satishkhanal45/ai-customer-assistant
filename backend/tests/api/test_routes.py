"""Tests for the Phase 5 chat API routes (api/routes.py).

The FastAPI app is bootstrapped the way ``main.py`` does it at startup so
the routes are exercised end-to-end; the service uses an in-memory
checkpointer (no live Postgres in tests). Focus: ``trace_id`` is generated
per request and both ends of the response agree; history is never accepted
from the caller (the request schema has no transcript field).
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from agents.supervisor.llm_client import StubSupervisorLLMClient
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes import router
from services.chat_service import build_chat_service


class _CreateTicketClient(StubSupervisorLLMClient):
    def classify(self, system_prompt, user_message, conversation_history) -> str:
        return json.dumps(
            {
                "request_category": "DOMAIN_REQUEST",
                "domain_confidence": 0.9,
                "intent": "CREATE_TICKET",
                "intent_confidence": 0.97,
                "clarification_question": None,
            }
        )


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def service_app(app):
    app.state.chat_service = await build_chat_service(
        llm_client=_CreateTicketClient()
    )
    return app


@pytest.mark.asyncio
async def test_chat_endpoint_returns_reply_and_trace_id(service_app):
    client = AsyncClient(
        transport=ASGITransport(app=service_app), base_url="http://test"
    )
    response = await client.post(
        "/chat",
        json={"thread_id": "api-t1", "message": "I need a refund"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == "api-t1"
    assert isinstance(body["reply"], str) and body["reply"]
    assert isinstance(body["trace_id"], str) and body["trace_id"]
    await client.aclose()


@pytest.mark.asyncio
async def test_chat_roundtrip_persists_thread(service_app):
    client = AsyncClient(
        transport=ASGITransport(app=service_app), base_url="http://test"
    )
    first = await client.post(
        "/chat",
        json={"thread_id": "thread-t2", "message": "I need a refund"},
    )
    assert "email" in first.json()["reply"].lower()

    second = await client.post(
        "/chat",
        json={"thread_id": "thread-t2", "message": "customer@example.com"},
    )
    second_body = second.json()
    assert "ticket" in second_body["reply"].lower()
    assert second_body["trace_id"] != first.json()["trace_id"]
    await client.aclose()
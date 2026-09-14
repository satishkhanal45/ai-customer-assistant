"""Chat without an account.

Chat is the public front door: someone arrives at the URL and starts asking,
with no signup in the way. An account anyone could create in ten seconds was
never access control -- it was friction pretending to be a gate -- so it was
removed rather than defended.

What replaces the gate is the quota, and that is what most of this file is
about. An open endpoint that spends model tokens per request is a spending
risk before it is a security one.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes import router
from auth import rate_limit


@pytest.fixture
def app():
    """No `as_role`: these tests are specifically about having no account."""
    application = FastAPI()
    application.include_router(router)
    return application


@pytest_asyncio.fixture
async def client(app, session_factory, monkeypatch):
    import db.engine

    # `enforce` opens its own transaction, deliberately, so a throttled
    # request's count is not rolled back with it -- which means it reaches for
    # the real engine. Point it at the test's.
    monkeypatch.setattr(db.engine, "get_session_factory", lambda: session_factory)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _ask(client, message="hello"):
    return await client.post("/chat", json={"thread_id": "t", "message": message})


class TestAnonymousAccess:
    async def test_chat_does_not_require_an_account(self, client, monkeypatch):
        """The point of the change. A 401 or 403 here means someone put the
        gate back."""
        monkeypatch.setenv(rate_limit.DISABLE_ENV, "true")

        response = await _ask(client)

        assert response.status_code not in (401, 403)

    async def test_the_stream_endpoint_is_public_too(self, client, monkeypatch):
        """Both, or the UI falls back to the buffered path for every visitor
        and the progress reporting is dead weight."""
        monkeypatch.setenv(rate_limit.DISABLE_ENV, "true")

        response = await client.post(
            "/chat/stream", json={"thread_id": "t", "message": "hello"}
        )

        assert response.status_code not in (401, 403)


class TestTheQuotaReplacesTheGate:
    async def test_an_anonymous_caller_is_limited_by_address(self, client, monkeypatch):
        """With no account there is no account id to count against, so the
        address is the key. Weaker -- shared behind NAT, rotated with a VPN --
        which is exactly why the global ceiling below also exists."""
        monkeypatch.delenv(rate_limit.DISABLE_ENV, raising=False)
        # `Limit` is frozen, so the whole object is replaced. A distinct scope
        # name keeps each test's bucket to itself.
        monkeypatch.setattr(
            rate_limit, "CHAT_PER_MINUTE",
            rate_limit.Limit("t-ip", 2, timedelta(minutes=1)),
        )

        codes = [(await _ask(client)).status_code for _ in range(3)]

        assert codes[-1] == 429
        assert 429 not in codes[:-1]

    async def test_the_global_ceiling_stops_the_deployment_overspending(
        self, client, monkeypatch
    ):
        """The limit that actually protects a public endpoint.

        One measured turn costs 6,871 tokens against a free tier allowing
        8,000 a minute. Per-caller limits bound an ordinary user; a determined
        one rotates addresses and walks past them while each per-IP counter
        reads as untouched.
        """
        monkeypatch.delenv(rate_limit.DISABLE_ENV, raising=False)
        monkeypatch.setattr(
            rate_limit, "CHAT_GLOBAL_PER_DAY",
            rate_limit.Limit("t-global", 2, timedelta(days=1)),
        )
        # Without this the forwarded header is ignored -- correctly, since
        # anyone can send it -- and every request would share one address. The
        # test would then pass on the per-caller limit and prove nothing about
        # rotation, which is the whole scenario.
        monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
        monkeypatch.setattr(
            rate_limit, "CHAT_PER_MINUTE",
            rate_limit.Limit("t-global-percaller", 50, timedelta(minutes=1)),
        )

        # A different address every time, so the per-caller limit never fires.
        codes = []
        for i in range(3):
            codes.append(
                (
                    await client.post(
                        "/chat",
                        json={"thread_id": "t", "message": "hi"},
                        headers={"x-forwarded-for": f"10.0.0.{i}"},
                    )
                ).status_code
            )

        assert 429 not in codes[:2], "ceiling fired too early"
        assert codes[-1] == 429, "a rotating caller walked past the spend ceiling"

    async def test_a_throttled_caller_is_told_how_long_to_wait(
        self, client, monkeypatch
    ):
        """Without `Retry-After` a client that retries immediately turns one
        throttled caller into a busy loop against the limiter."""
        monkeypatch.delenv(rate_limit.DISABLE_ENV, raising=False)
        monkeypatch.setattr(
            rate_limit, "CHAT_PER_MINUTE",
            rate_limit.Limit("t-retry", 1, timedelta(minutes=1)),
        )

        await _ask(client)
        throttled = await _ask(client)

        assert throttled.status_code == 429
        assert "retry-after" in throttled.headers


class TestSignupIsGone:
    def test_the_signup_endpoint_no_longer_exists(self):
        """It answered "how does a stranger get an account?", and the public
        front door makes that question stop existing.

        Checked against the auth router rather than the assembled app on
        purpose. Importing `main` runs `config.load_env()` at module scope,
        which injects every value in `backend/.env` into the process -- that
        is P0-2, and it does not stay contained: a later test builds the real
        chat stack, sees `POSTGRES_HOST=postgres`, and spends thirty seconds
        failing to resolve a Docker hostname.
        """
        from auth.router import router as auth_router

        paths = {r.path for r in auth_router.routes if hasattr(r, "path")}
        assert "/auth/signup" not in paths
        assert "/auth/login" in paths, "sanity: the router really was inspected"

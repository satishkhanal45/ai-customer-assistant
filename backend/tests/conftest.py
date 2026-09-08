"""Shared fixtures for the whole suite, most of them about authentication.

These live at the root rather than under `tests/auth/` because every test
that drives an HTTP endpoint now needs them: since P0-3 the chat, graph and
ingest routers all require an authenticated caller, so "build an app and
call it" is no longer something a test can do without an identity.

Everything here runs against an in-memory SQLite database and a FastAPI app
assembled per test, so the suite needs neither Postgres nor a signing secret
in the developer's environment.

`AUTH_SECRET` is set by an autouse fixture rather than by the developer,
because `auth.tokens` deliberately refuses to issue anything without one --
the same refusal that makes a misconfigured deployment fail at boot makes an
unconfigured test process fail at import, and that is the correct behaviour
in both cases.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from auth import passwords, roles
from db.models import AppUser, RateLimitBucket, RefreshToken

TEST_SECRET = "test-secret-that-is-long-enough-for-hs256!!"
TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", TEST_SECRET)
    # Cookies over the ASGI transport are not on a secure origin, and
    # httpx honours the Secure attribute, so a secure cookie would be set
    # and then never sent back -- every authenticated request would 401 for
    # a reason that has nothing to do with the code under test.
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    # Rate limits are exercised by their own tests, against a session they
    # control. Leaving them on here would make every other test in this file
    # depend on the shared limiter's table.
    monkeypatch.setenv("RATE_LIMIT_DISABLED", "1")


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            AppUser.__table__,
            RefreshToken.__table__,
            RateLimitBucket.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def make_user(session_factory):
    """Create an account. Returns the persisted `AppUser`."""

    async def _make(
        email: str = "member@example.com",
        *,
        password: str | None = TEST_PASSWORD,
        role: str = roles.MEMBER,
        is_active: bool = True,
        is_service_account: bool = False,
    ) -> AppUser:
        async with session_factory() as session:
            user = AppUser(
                id=uuid.uuid4(),
                email=email,
                password_hash=(
                    passwords.hash_password(password) if password else None
                ),
                role=role,
                is_active=is_active,
                is_service_account=is_service_account,
                created_at=datetime.now(timezone.utc),
            )
            session.add(user)
            await session.commit()
            return user

    return _make


@pytest.fixture
def build_app(session_factory, monkeypatch):
    """An app whose `get_session` yields sessions from the test database.

    The dependency is overridden rather than the engine patched, because
    `get_session` is what every route actually depends on and overriding it
    is the same mechanism a caller would use.
    """
    from db.engine import get_session

    def _build(*routers, **kwargs) -> FastAPI:
        app = FastAPI(**kwargs)
        for router in routers:
            app.include_router(router)

        async def _test_session():
            async with session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        app.dependency_overrides[get_session] = _test_session
        return app

    return _build


@pytest_asyncio.fixture
async def client(build_app):
    """An unauthenticated client against the full auth router."""
    from auth.router import router as auth_router

    app = build_app(auth_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


@pytest.fixture
def as_role():
    """Sign a test app in as somebody, without going through a login.

    Tests that are *about* authentication drive the real endpoints; tests
    that merely need to reach a now-protected route should not have to
    create an account and log in to do it. This overrides
    `get_current_user`, which every role dependency and every quota guard
    resolves through, so one override covers all of them.

        def test_something(build_app, as_role):
            app = build_app(some_router)
            as_role(app)                       # a member
            as_role(app, role=roles.ADMIN)     # or an admin
    """
    from auth.dependencies import Principal, get_current_user

    def _apply(
        app,
        *,
        role: str = roles.MEMBER,
        email: str = "test-principal@example.com",
        user_id: uuid.UUID | None = None,
    ) -> Principal:
        principal = Principal(id=user_id or uuid.uuid4(), email=email, role=role)
        app.dependency_overrides[get_current_user] = lambda: principal
        return principal

    return _apply

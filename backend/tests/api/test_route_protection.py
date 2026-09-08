"""Every route is either deliberately public or requires a credential.

This is the test that matters most in the whole authentication change, and
it is deliberately not a list of endpoints someone has to remember to
update. It enumerates the routes of the *assembled application* and asserts
each one either appears on a small literal allowlist or answers 401 without
a credential.

The failure it exists to catch is not a bug in the code below -- it is the
endpoint somebody adds in six months and forgets to protect. A per-endpoint
test suite cannot catch that, because the missing test is exactly the one
nobody wrote. This is the same shape as `assert_ladder_is_consistent()` in
`timeouts.py`, which exists because checking each timeout individually
missed the fact that they summed to more than the client's patience.

The allowlist is three routes and it is a literal, so widening it is a
visible diff in a review rather than an omission nobody sees.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

# The complete set of routes that may be reached without a credential.
#
#   /health        liveness probes must not need to authenticate
#   /auth/login    obviously
#   /auth/refresh  must work when the access token has already expired
#   /auth/logout   must work then too, and revoking a session is not an
#                  action an attacker gains anything from
PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/health"),
        ("POST", "/auth/login"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/logout"),
    }
)

# Placeholders for path parameters. The value never matters: an unauthenticated
# request must be refused before anything looks at it, and a route that
# validates its path before checking the credential would leak the difference
# between a real and an invented id.
_PATH_VALUES = {
    "job_id": "00000000-0000-0000-0000-000000000000",
    "entity_id": "00000000-0000-0000-0000-000000000000",
    "discovery_id": "not-a-real-discovery",
}


@pytest.fixture
def app(monkeypatch, session_factory):
    """The real application, assembled the way `main.py` assembles it.

    `config.load_env` is neutered before the import. `main` calls it at
    module scope -- correctly, it is the process entry point -- but in a test
    process it would inject every value from `backend/.env` into `os.environ`
    and change the behaviour of unrelated tests that read it. That is the
    same defect P0-2 fixed in `agents/ticket_agent/store.py`, and importing
    the entry point is the one place it is legitimate, so it is neutralised
    here rather than avoided.
    """
    import config

    monkeypatch.setattr(config, "load_env", lambda *a, **k: False)
    for key, value in {
        "POSTGRES_USER": "test",
        "POSTGRES_PASSWORD": "test",
        "POSTGRES_DB": "test",
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5433",
    }.items():
        monkeypatch.setenv(key, value)

    import main
    from db.engine import get_session

    async def _test_session():
        async with session_factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = _test_session
    yield main.app
    main.app.dependency_overrides.clear()


def _routes(app) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, including nested routers.

    The walk is recursive because `include_router` does not flatten: an
    included router appears in `app.routes` as a single object holding its
    own `.routes`. A non-recursive version of this function sees only
    `/health` and reports a completely unprotected API as fully protected --
    which is exactly the vacuous pass `test_the_application_has_the_routes_
    this_test_thinks_it_has` exists to catch, and did.
    """
    found: list[tuple[str, str]] = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    found.append((method, route.path))
            # An included router appears as a wrapper object, and which
            # attribute holds the real one has moved between FastAPI
            # versions (`routes` on older ones, `original_router` on 0.141).
            # Both are followed rather than pinned, because the failure mode
            # of guessing wrong is silent: the walk returns almost nothing
            # and every assertion below passes vacuously.
            nested = getattr(route, "routes", None)
            if nested is None:
                inner = getattr(route, "original_router", None)
                nested = getattr(inner, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    return sorted(set(found))


def _concrete(path: str) -> str:
    for name, value in _PATH_VALUES.items():
        path = path.replace("{" + name + "}", value)
    return path


def test_the_application_has_the_routes_this_test_thinks_it_has(app):
    """A guard on the guard.

    If the app were assembled without its routers, every assertion below
    would pass vacuously and the suite would report that an unprotected API
    was fully protected.
    """
    routes = _routes(app)
    assert len(routes) >= 14, routes
    paths = {path for _, path in routes}
    assert "/chat" in paths
    assert "/graph/search" in paths
    assert "/ingest/upload" in paths
    assert "/auth/login" in paths


@pytest.mark.asyncio
async def test_every_route_is_public_by_declaration_or_refuses_anonymous(app):
    """The whole point of this file."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unprotected = []
        for method, path in _routes(app):
            if (method, path) in PUBLIC_ROUTES:
                continue
            response = await client.request(
                method, _concrete(path), json={}, headers={"content-type": "application/json"}
            )
            if response.status_code not in (401, 403):
                unprotected.append((method, path, response.status_code))

    assert not unprotected, (
        "These routes answered an unauthenticated caller with something other "
        "than 401/403. Either they need a role dependency, or they belong on "
        f"PUBLIC_ROUTES with a reason: {unprotected}"
    )


def test_the_public_allowlist_stays_small(app):
    """Every entry here is a route anyone on the internet can reach, so the
    list is worth being hard to grow by accident."""
    assert len(PUBLIC_ROUTES) == 4
    assert all(path.startswith(("/health", "/auth/")) for _, path in PUBLIC_ROUTES)


def test_public_routes_actually_exist(app):
    """A typo in the allowlist would silently exempt nothing while looking
    like it exempted something -- or worse, mask a route that was renamed."""
    declared = set(_routes(app))
    missing = PUBLIC_ROUTES - declared
    assert not missing, f"PUBLIC_ROUTES names routes that do not exist: {missing}"


@pytest.mark.asyncio
async def test_health_is_reachable_without_a_credential(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200

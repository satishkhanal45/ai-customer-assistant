"""What the dependencies let through, and what they do not.

The interesting cases are the ones where a *valid* credential must still be
refused: a deactivated account, a member reaching an admin route, and a role
that was changed after the token was issued.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient

from auth import roles, tokens
from auth.cookies import ACCESS_COOKIE
from auth.dependencies import Principal, get_current_user, require_admin, require_member

protected = APIRouter()


@protected.get("/anyone")
async def anyone(principal: Principal = Depends(get_current_user)) -> dict:
    return {"email": principal.email, "role": principal.role}


@protected.get("/members-only")
async def members_only(principal: Principal = Depends(require_member)) -> dict:
    return {"ok": True}


@protected.get("/admins-only")
async def admins_only(principal: Principal = Depends(require_admin)) -> dict:
    return {"ok": True}


@pytest_asyncio.fixture
async def api(build_app):
    app = build_app(protected)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def _bearer(user) -> dict:
    token, _ = tokens.issue_access_token(user.id, user.role)
    return {"Authorization": f"Bearer {token}"}


class TestTransports:
    async def test_a_bearer_header_authenticates(self, api, make_user):
        user = await make_user()
        response = await api.get("/anyone", headers=_bearer(user))
        assert response.status_code == 200
        assert response.json()["email"] == user.email

    async def test_a_cookie_authenticates(self, api, make_user):
        user = await make_user()
        token, _ = tokens.issue_access_token(user.id, user.role)
        api.cookies.set(ACCESS_COOKIE, token)

        assert (await api.get("/anyone")).status_code == 200

    async def test_no_credential_is_a_401_with_a_challenge(self, api):
        """The WWW-Authenticate header is what tells the frontend to try a
        refresh before giving up and showing the login page."""
        response = await api.get("/anyone")
        assert response.status_code == 401
        assert "www-authenticate" in response.headers

    async def test_a_malformed_authorization_header_is_a_401(self, api):
        response = await api.get("/anyone", headers={"Authorization": "Basic abc"})
        assert response.status_code == 401


class TestAccountState:
    async def test_a_deactivated_account_is_forbidden(self, api, make_user):
        """403, not 401: the credential is genuine, so refreshing will not
        help and the client must not loop trying."""
        user = await make_user(is_active=False)
        response = await api.get("/anyone", headers=_bearer(user))
        assert response.status_code == 403

    async def test_a_token_for_a_deleted_account_is_a_401(self, api, make_user):
        ghost = type("Ghost", (), {"id": uuid.uuid4(), "role": roles.MEMBER})()
        assert (await api.get("/anyone", headers=_bearer(ghost))).status_code == 401

    async def test_the_role_comes_from_the_row_not_the_token(
        self, api, make_user, session_factory
    ):
        """A demotion must take effect immediately. If the role were read
        from the token, a demoted admin would keep admin access until their
        fifteen-minute token happened to expire."""
        from db.models import AppUser

        user = await make_user(role=roles.ADMIN)
        header = _bearer(user)
        assert (await api.get("/admins-only", headers=header)).status_code == 200

        async with session_factory() as session:
            row = await session.get(AppUser, user.id)
            row.role = roles.MEMBER
            await session.commit()

        # Same token, still validly signed, still says role=admin.
        assert (await api.get("/admins-only", headers=header)).status_code == 403


class TestRoleOrdering:
    async def test_a_member_passes_a_member_check(self, api, make_user):
        user = await make_user(role=roles.MEMBER)
        assert (await api.get("/members-only", headers=_bearer(user))).status_code == 200

    async def test_an_admin_passes_a_member_check(self, api, make_user):
        """The ordering is the point: admin satisfies member without anyone
        having to remember to list it."""
        user = await make_user(role=roles.ADMIN)
        assert (await api.get("/members-only", headers=_bearer(user))).status_code == 200

    async def test_a_member_fails_an_admin_check(self, api, make_user):
        user = await make_user(role=roles.MEMBER)
        assert (await api.get("/admins-only", headers=_bearer(user))).status_code == 403


class TestRoles:
    def test_an_unknown_role_raises_rather_than_defaulting(self):
        """Never silently "some access". A typo or a corrupted row is worth
        crashing over rather than guessing about."""
        with pytest.raises(roles.UnknownRoleError):
            roles.satisfies("superuser", roles.MEMBER)

    def test_the_ordering_is_ascending(self):
        assert roles.ALL_ROLES == (roles.MEMBER, roles.ADMIN)

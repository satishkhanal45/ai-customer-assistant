"""The /auth endpoints: login, rotation, logout, and account creation.

Two properties get the most attention, because they are the ones that are
easy to get subtly wrong and impossible to notice afterwards:

* **Login says the same thing however it fails.** A response that
  distinguishes "no such account" from "wrong password" is an account
  enumeration oracle, and knowing which addresses have accounts is most of
  the work of a targeted attack.
* **Refresh rotates, and reuse is treated as theft.** A refresh token
  presented twice is either a race or a stolen copy; the response to the
  second presentation is to revoke every session the user has.
"""
from __future__ import annotations

import uuid

import pytest

from auth import roles
from auth.cookies import ACCESS_COOKIE, REFRESH_COOKIE
from tests.conftest import TEST_PASSWORD


async def _login(client, email="member@example.com", password=TEST_PASSWORD, **extra):
    return await client.post(
        "/auth/login", json={"email": email, "password": password, **extra}
    )


class TestLogin:
    async def test_valid_credentials_set_both_cookies(self, client, make_user):
        await make_user()
        response = await _login(client)

        assert response.status_code == 200
        assert ACCESS_COOKIE in response.cookies
        assert REFRESH_COOKIE in response.cookies

    async def test_the_cookie_transport_keeps_tokens_out_of_the_body(
        self, client, make_user
    ):
        """The whole point of the cookie transport. The frontend renders
        model output into the DOM, so a token any script can read is a token
        an injected script can steal."""
        await make_user()
        body = (await _login(client)).json()

        assert body["access_token"] is None
        assert body["refresh_token"] is None
        assert body["user"]["email"] == "member@example.com"

    async def test_cookies_are_httponly(self, client, make_user):
        await make_user()
        response = await _login(client)
        set_cookie_headers = " ".join(
            value for key, value in response.headers.multi_items()
            if key.lower() == "set-cookie"
        ).lower()

        assert set_cookie_headers.count("httponly") == 2
        assert "samesite=strict" in set_cookie_headers

    async def test_the_bearer_transport_returns_tokens_and_no_cookies(
        self, client, make_user
    ):
        """Scripts have no cookie jar worth the name. Asking for the token
        explicitly is what keeps it out of the browser's response body."""
        await make_user()
        response = await _login(client, transport="bearer")
        body = response.json()

        assert body["access_token"] and body["refresh_token"]
        assert ACCESS_COOKIE not in response.cookies

    async def test_a_wrong_password_is_rejected(self, client, make_user):
        await make_user()
        response = await _login(client, password="not-the-password")
        assert response.status_code == 401

    async def test_an_unknown_account_is_rejected_identically(
        self, client, make_user
    ):
        """Same status, same message. Anything else tells the caller which
        half of the credential they got right."""
        await make_user()
        wrong_password = await _login(client, password="not-the-password")
        no_such_user = await _login(client, email="nobody@example.com")

        assert no_such_user.status_code == wrong_password.status_code
        assert no_such_user.json()["detail"] == wrong_password.json()["detail"]

    async def test_a_deactivated_account_cannot_log_in(self, client, make_user):
        await make_user(is_active=False)
        response = await _login(client)
        assert response.status_code == 403

    async def test_a_service_account_cannot_log_in(self, client, make_user):
        """The seeded service account owns every previously ingested
        document. It exists to be a foreign key, not to sign in."""
        await make_user(is_service_account=True)
        assert (await _login(client)).status_code == 403

    async def test_an_account_with_no_password_cannot_log_in(
        self, client, make_user
    ):
        await make_user(password=None)
        assert (await _login(client, password="anything-at-all")).status_code == 401


class TestRefresh:
    async def test_refresh_issues_a_new_pair(self, client, make_user):
        await make_user()
        await _login(client)
        first_access = client.cookies[ACCESS_COOKIE]

        response = await client.post("/auth/refresh", json={})

        assert response.status_code == 200
        assert client.cookies[ACCESS_COOKIE] != first_access

    async def test_a_refresh_token_is_single_use(self, client, make_user):
        """Rotation. Without it a stolen refresh token stays valid for its
        full fourteen days alongside the real one, invisibly."""
        await make_user()
        await _login(client)
        stolen = client.cookies[REFRESH_COOKIE]

        assert (await client.post("/auth/refresh", json={})).status_code == 200

        replayed = await client.post(
            "/auth/refresh", json={"refresh_token": stolen}
        )
        assert replayed.status_code == 401

    async def test_reuse_revokes_every_session_for_that_user(
        self, client, make_user, session_factory
    ):
        """A token presented twice means someone has a copy. Revoking only
        the replayed token would leave the thief's *new* pair working."""
        from sqlalchemy import select

        from db.models import RefreshToken

        user = await make_user()
        await _login(client)
        stolen = client.cookies[REFRESH_COOKIE]
        await client.post("/auth/refresh", json={})

        await client.post("/auth/refresh", json={"refresh_token": stolen})

        async with session_factory() as session:
            live = (
                await session.execute(
                    select(RefreshToken).where(
                        RefreshToken.user_id == user.id,
                        RefreshToken.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
        assert live == []

    async def test_an_access_token_cannot_be_used_to_refresh(
        self, client, make_user
    ):
        await make_user()
        body = (await _login(client, transport="bearer")).json()

        response = await client.post(
            "/auth/refresh", json={"refresh_token": body["access_token"]}
        )
        assert response.status_code == 401

    async def test_refresh_without_a_token_is_rejected(self, client):
        assert (await client.post("/auth/refresh", json={})).status_code == 401


class TestLogout:
    async def test_logout_clears_the_cookies(self, client, make_user):
        await make_user()
        await _login(client)

        response = await client.post("/auth/logout")

        assert response.status_code == 204
        assert not client.cookies.get(ACCESS_COOKIE)

    async def test_the_refresh_token_stops_working(self, client, make_user):
        """A logout that only deletes the client's copy is theatre. The
        server must forget it too."""
        await make_user()
        await _login(client)
        old_refresh = client.cookies[REFRESH_COOKIE]

        await client.post("/auth/logout")

        replayed = await client.post(
            "/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert replayed.status_code == 401

    async def test_logout_works_without_a_session(self, client):
        """It must succeed when the access token has already expired, which
        is exactly when someone is most likely to click it."""
        assert (await client.post("/auth/logout")).status_code == 204


class TestMe:
    async def test_me_returns_the_signed_in_user(self, client, make_user):
        await make_user(email="sam@example.com", role=roles.ADMIN)
        await _login(client, email="sam@example.com")

        body = (await client.get("/auth/me")).json()

        assert body["email"] == "sam@example.com"
        assert body["role"] == roles.ADMIN

    async def test_me_requires_a_credential(self, client):
        assert (await client.get("/auth/me")).status_code == 401


class TestCreateUser:
    async def test_an_admin_can_create_an_account(self, client, make_user):
        await make_user(email="boss@example.com", role=roles.ADMIN)
        await _login(client, email="boss@example.com")

        response = await client.post(
            "/auth/users",
            json={
                "email": "New.Person@Example.com",
                "password": "another-long-enough-password",
                "role": roles.MEMBER,
            },
        )

        assert response.status_code == 201
        # Stored lowercase, so a login cannot be defeated by capitalisation
        # and two rows cannot exist for one address.
        assert response.json()["email"] == "new.person@example.com"

    async def test_a_member_cannot_create_an_account(self, client, make_user):
        """There is no self-signup. An open registration endpoint here would
        be a way to mint yourself an account and read the whole corpus."""
        await make_user()
        await _login(client)

        response = await client.post(
            "/auth/users",
            json={"email": "x@example.com", "password": "another-long-password"},
        )
        assert response.status_code == 403

    async def test_creating_an_account_requires_authentication(self, client):
        response = await client.post(
            "/auth/users",
            json={"email": "x@example.com", "password": "another-long-password"},
        )
        assert response.status_code == 401

    async def test_a_duplicate_email_is_a_conflict(self, client, make_user):
        await make_user(email="boss@example.com", role=roles.ADMIN)
        await make_user(email="taken@example.com")
        await _login(client, email="boss@example.com")

        response = await client.post(
            "/auth/users",
            json={"email": "taken@example.com", "password": "another-long-password"},
        )
        assert response.status_code == 409

    async def test_an_unknown_role_is_refused(self, client, make_user):
        await make_user(email="boss@example.com", role=roles.ADMIN)
        await _login(client, email="boss@example.com")

        response = await client.post(
            "/auth/users",
            json={
                "email": "x@example.com",
                "password": "another-long-password",
                "role": "superuser",
            },
        )
        assert response.status_code == 422

    async def test_a_weak_password_is_refused(self, client, make_user):
        await make_user(email="boss@example.com", role=roles.ADMIN)
        await _login(client, email="boss@example.com")

        response = await client.post(
            "/auth/users", json={"email": "x@example.com", "password": "short"}
        )
        assert response.status_code == 422

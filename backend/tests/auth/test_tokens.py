"""Token issuing and, mostly, token *refusing*.

The interesting assertions here are all negative. A token verifier is only
worth anything for what it rejects, and the ways it can be wrong are
well known enough to be enumerated: `alg: none`, a token signed with a
different key, a refresh token presented where an access token belongs, and
an expired token that still parses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from auth import tokens


@pytest.fixture
def subject() -> uuid.UUID:
    return uuid.uuid4()


class TestRoundTrip:
    def test_access_token_carries_subject_and_role(self, subject):
        token, claims = tokens.issue_access_token(subject, "member")
        decoded = tokens.decode_token(token, expected_type=tokens.ACCESS_TYPE)

        assert decoded.subject == subject
        assert decoded.role == "member"
        assert decoded.jti == claims.jti

    def test_each_token_has_a_distinct_id(self, subject):
        """The jti is what a revocation record points at, so two tokens
        sharing one would make revoking either revoke both."""
        first, _ = tokens.issue_refresh_token(subject, "member")
        second, _ = tokens.issue_refresh_token(subject, "member")

        assert (
            tokens.decode_token(first, expected_type=tokens.REFRESH_TYPE).jti
            != tokens.decode_token(second, expected_type=tokens.REFRESH_TYPE).jti
        )

    def test_lifetimes_are_the_documented_ones(self, subject):
        _, access = tokens.issue_access_token(subject, "member")
        _, refresh = tokens.issue_refresh_token(subject, "member")

        assert access.expires_at - access.issued_at == timedelta(minutes=15)
        assert refresh.expires_at - refresh.issued_at == timedelta(days=14)


class TestRefusals:
    def test_alg_none_is_rejected(self, subject):
        """The classic JWT forgery: strip the signature and claim the token
        says it needs none. `decode` is always given an explicit algorithm
        list, which is what makes this fail."""
        forged = jwt.encode(
            {
                "sub": str(subject),
                "jti": str(uuid.uuid4()),
                "iat": 0,
                "exp": 9_999_999_999,
                "typ": tokens.ACCESS_TYPE,
                "role": "admin",
            },
            "",
            algorithm="none",
        )
        with pytest.raises(tokens.InvalidTokenError):
            tokens.decode_token(forged, expected_type=tokens.ACCESS_TYPE)

    def test_a_token_signed_with_another_key_is_rejected(self, subject):
        forged = jwt.encode(
            {
                "sub": str(subject),
                "jti": str(uuid.uuid4()),
                "iat": 0,
                "exp": 9_999_999_999,
                "typ": tokens.ACCESS_TYPE,
                "role": "admin",
            },
            "a-different-secret-of-adequate-length-1234",
            algorithm="HS256",
        )
        with pytest.raises(tokens.InvalidTokenError):
            tokens.decode_token(forged, expected_type=tokens.ACCESS_TYPE)

    def test_a_refresh_token_is_not_an_access_token(self, subject):
        """Both are signed with the same key, so without the type check a
        14-day refresh token would authenticate every protected endpoint --
        exactly the long-lived bearer credential the 15-minute access TTL
        exists to avoid."""
        refresh, _ = tokens.issue_refresh_token(subject, "member")
        with pytest.raises(tokens.InvalidTokenError):
            tokens.decode_token(refresh, expected_type=tokens.ACCESS_TYPE)

    def test_an_access_token_is_not_a_refresh_token(self, subject):
        access, _ = tokens.issue_access_token(subject, "member")
        with pytest.raises(tokens.InvalidTokenError):
            tokens.decode_token(access, expected_type=tokens.REFRESH_TYPE)

    def test_expired_tokens_raise_the_expiry_error(self, subject):
        """Distinguished from InvalidTokenError only so the API can answer
        "refresh" rather than "log in again". It authenticates no one either
        way."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        token, _ = tokens.issue_access_token(subject, "member", now=past)

        with pytest.raises(tokens.ExpiredTokenError):
            tokens.decode_token(token, expected_type=tokens.ACCESS_TYPE)

    def test_garbage_is_rejected(self):
        with pytest.raises(tokens.InvalidTokenError):
            tokens.decode_token("not.a.token", expected_type=tokens.ACCESS_TYPE)

    def test_a_token_without_a_role_is_rejected(self, subject):
        """Every authorisation decision reads the role. A token that omits it
        must fail closed rather than arrive with `role=None` and be compared
        against the ordering."""
        forged = jwt.encode(
            {
                "sub": str(subject),
                "jti": str(uuid.uuid4()),
                "iat": 0,
                "exp": 9_999_999_999,
                "typ": tokens.ACCESS_TYPE,
            },
            tokens.auth_secret(),
            algorithm="HS256",
        )
        with pytest.raises(tokens.InvalidTokenError):
            tokens.decode_token(forged, expected_type=tokens.ACCESS_TYPE)


class TestSecretHandling:
    def test_no_secret_is_a_hard_failure(self, monkeypatch):
        """Never a default. A known signing key means anyone who has read the
        source can mint valid tokens for any account."""
        monkeypatch.delenv("AUTH_SECRET", raising=False)
        with pytest.raises(tokens.MissingSecretError):
            tokens.auth_secret()

    def test_a_short_secret_is_refused(self, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET", "too-short")
        with pytest.raises(tokens.MissingSecretError):
            tokens.auth_secret()

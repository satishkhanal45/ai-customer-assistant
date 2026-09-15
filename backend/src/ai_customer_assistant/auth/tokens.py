"""Issuing and decoding JWTs.

Two token types, deliberately asymmetric:

* **Access** — 15 minutes, stateless. Verification is a signature check, so
  the hot path costs no I/O of its own.
* **Refresh** — 14 days, *stateful*: its ``jti`` is stored, so it can be
  revoked. A purely stateless scheme cannot revoke anything, and a "logout"
  that only deletes the client's copy of a token is theatre.

``HS256`` with one secret is the right choice for a single backend service.
``RS256`` earns its key management only when some other service must verify
tokens without being able to mint them, which is not the case here.

## Two things this module refuses to do

**It will not run without a secret.** ``auth_secret()`` raises rather than
defaulting, and ``main.py`` calls it during startup so the failure is at boot
rather than at the first login attempt. This is the same discipline
``timeouts.py`` uses, for the same reason: a silently-defaulted signing key
is worse than a crash, because every token it issues is forgeable by anyone
who has read the source.

**It will not honour the token's own opinion about its algorithm.** ``decode``
is always given an explicit ``algorithms`` list, which is what rejects the
``{"alg": "none"}`` forgery. Leaving that to the library's default is the
single most common way JWT verification is got wrong.

The secret is resolved on use rather than at import, so importing this module
in a process that has no ``AUTH_SECRET`` — a unit test, say — is safe.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

import jwt

AUTH_SECRET_ENV: Final[str] = "AUTH_SECRET"
ALGORITHM: Final[str] = "HS256"

ACCESS_TOKEN_TTL: Final[timedelta] = timedelta(minutes=15)
REFRESH_TOKEN_TTL: Final[timedelta] = timedelta(days=14)

ACCESS_TYPE: Final[str] = "access"
REFRESH_TYPE: Final[str] = "refresh"

# 32 bytes of secret is the floor for HS256 to be worth anything; below that
# the signature is brute-forceable offline from a single captured token.
MIN_SECRET_LENGTH: Final[int] = 32


class TokenError(Exception):
    """Base class: the token cannot be trusted. Callers answer 401."""


class ExpiredTokenError(TokenError):
    """Well-formed, correctly signed, and past its expiry.

    Distinguished from ``InvalidTokenError`` only so the API can tell the
    client to refresh rather than to log in again. It is never a weaker
    check — an expired token authenticates no one.
    """


class InvalidTokenError(TokenError):
    """Malformed, badly signed, or not the token type that was asked for."""


class MissingSecretError(RuntimeError):
    """``AUTH_SECRET`` is absent or too short. The process must not serve."""


@dataclass(frozen=True)
class TokenClaims:
    """The claims this application actually relies on."""

    subject: uuid.UUID
    role: str
    token_type: str
    jti: uuid.UUID
    issued_at: datetime
    expires_at: datetime


def auth_secret() -> str:
    """The signing secret, or raise.

    Called at startup by ``main.py`` so a misconfigured deployment fails
    immediately and visibly, rather than at whatever hour the first person
    tries to log in.
    """
    secret = os.environ.get(AUTH_SECRET_ENV, "")
    if not secret:
        raise MissingSecretError(
            f"{AUTH_SECRET_ENV} is not set. Generate one with "
            f"`python -c 'import secrets; print(secrets.token_urlsafe(48))'` "
            f"and put it in the environment. There is deliberately no default: "
            f"a known signing key means anyone can mint valid tokens."
        )
    if len(secret) < MIN_SECRET_LENGTH:
        raise MissingSecretError(
            f"{AUTH_SECRET_ENV} is {len(secret)} characters; at least "
            f"{MIN_SECRET_LENGTH} are required for HS256 to resist an offline "
            f"attack on a single captured token."
        )
    return secret


def _issue(
    subject: uuid.UUID,
    role: str,
    *,
    token_type: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> tuple[str, TokenClaims]:
    issued_at = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    expires_at = issued_at + ttl
    claims = TokenClaims(
        subject=subject,
        role=role,
        token_type=token_type,
        jti=uuid.uuid4(),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    encoded = jwt.encode(
        {
            "sub": str(claims.subject),
            "role": claims.role,
            "typ": claims.token_type,
            "jti": str(claims.jti),
            "iat": int(claims.issued_at.timestamp()),
            "exp": int(claims.expires_at.timestamp()),
        },
        auth_secret(),
        algorithm=ALGORITHM,
    )
    return encoded, claims


def issue_access_token(
    subject: uuid.UUID, role: str, *, now: datetime | None = None
) -> tuple[str, TokenClaims]:
    return _issue(
        subject, role, token_type=ACCESS_TYPE, ttl=ACCESS_TOKEN_TTL, now=now
    )


def issue_refresh_token(
    subject: uuid.UUID, role: str, *, now: datetime | None = None
) -> tuple[str, TokenClaims]:
    return _issue(
        subject, role, token_type=REFRESH_TYPE, ttl=REFRESH_TOKEN_TTL, now=now
    )


def decode_token(token: str, *, expected_type: str) -> TokenClaims:
    """Verify a token and return its claims, or raise a ``TokenError``.

    ``expected_type`` is not decoration. Access and refresh tokens are signed
    with the same key, so without this check a 14-day refresh token would be
    accepted as a credential on every protected endpoint — which is exactly
    the long-lived bearer token the short access TTL exists to avoid.
    """
    try:
        payload = jwt.decode(
            token,
            auth_secret(),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredTokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, an unexpected algorithm (including
        # `alg: none`), malformed segments, and missing required claims.
        raise InvalidTokenError(f"Token is not valid: {exc}") from exc

    token_type = payload.get("typ")
    if token_type != expected_type:
        raise InvalidTokenError(
            f"Expected a {expected_type} token, got {token_type!r}."
        )

    try:
        subject = uuid.UUID(payload["sub"])
        jti = uuid.UUID(payload["jti"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError("Token subject or id is not a uuid.") from exc

    role = payload.get("role")
    if not isinstance(role, str) or not role:
        raise InvalidTokenError("Token carries no role.")

    return TokenClaims(
        subject=subject,
        role=role,
        token_type=token_type,
        jti=jti,
        issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )

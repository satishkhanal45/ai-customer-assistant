"""The FastAPI dependencies that enforce authentication and authorisation.

One credential format, two transports. ``get_current_user`` reads the access
token from the ``access_token`` cookie first and from an
``Authorization: Bearer`` header second, so the browser app and a ``curl``
script authenticate through the same verifier and the same code path. There
is no second implementation to keep in step.

## The database lookup

Verifying the signature needs no I/O, but this dependency loads the user row
anyway, to check ``is_active``. That is a deliberate trade: it costs one
indexed primary-key lookup per request, and it buys deactivation that takes
effect immediately instead of up to fifteen minutes later. Against a chat
turn with a 52-second budget and four LLM calls in it, the lookup is not
measurable; an offboarded account that keeps working for another quarter of
an hour is.

## Failure codes

* **401** — no credential, or one that cannot be verified. The response
  carries ``WWW-Authenticate``, which is what tells the frontend to try a
  refresh before giving up and showing the login page.
* **403** — a valid credential belonging to someone who may not do this.
  Refreshing will not help, and the frontend must not retry.

Keeping those apart matters: answering 401 for an authorisation failure
sends the client into a refresh-and-replay loop that can never succeed.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth import rate_limit, roles, tokens
from auth.cookies import ACCESS_COOKIE
from db.engine import get_session

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "bearer "


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as the rest of the application sees them."""

    id: uuid.UUID
    email: str
    role: str

    @property
    def is_admin(self) -> bool:
        return roles.satisfies(self.role, roles.ADMIN)


def _unauthenticated(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="api"'},
    )


def extract_token(request: Request) -> str | None:
    """The access token from the cookie, else the Authorization header.

    Cookie first because the browser is the common case and its cookie is
    sent automatically; the header is what scripts and ``curl`` use.
    """
    cookie = request.cookies.get(ACCESS_COOKIE)
    if cookie:
        return cookie

    header = request.headers.get("authorization", "")
    if header.lower().startswith(_BEARER_PREFIX):
        candidate = header[len(_BEARER_PREFIX):].strip()
        return candidate or None
    return None


async def _principal_from_token(token: str, session: AsyncSession) -> Principal:
    from db.models import AppUser

    try:
        claims = tokens.decode_token(token, expected_type=tokens.ACCESS_TYPE)
    except tokens.ExpiredTokenError as exc:
        raise _unauthenticated("Access token has expired.") from exc
    except tokens.TokenError as exc:
        # The message is deliberately not echoed back: it would tell an
        # attacker which part of a forged token was rejected.
        logger.debug("Rejected access token: %s", exc)
        raise _unauthenticated("Invalid access token.") from exc

    user = await session.get(AppUser, claims.subject)
    if user is None:
        # A correctly signed token for a user who no longer exists. The
        # signature was valid, so this is worth a log line.
        logger.warning("Valid token for unknown user %s.", claims.subject)
        raise _unauthenticated("Unknown account.")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is deactivated.",
        )

    # The role comes from the row, not from the token. A demotion must take
    # effect now, not when the fifteen-minute token happens to expire.
    return Principal(id=user.id, email=user.email, role=user.role)


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """The authenticated caller, or 401. The base of every protected route."""
    token = extract_token(request)
    if not token:
        raise _unauthenticated("Not authenticated.")
    principal = await _principal_from_token(token, session)
    # Stashed for the rate limiter and the logging tracer, which run outside
    # the dependency graph and cannot ask for this any other way.
    request.state.principal = principal
    return principal


async def get_optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal | None:
    """The caller if they are authenticated, ``None`` if not.

    For routes that are public but behave differently for a known caller.
    Nothing uses it today; it exists so that adding such a route does not
    tempt anyone into catching ``HTTPException`` around ``get_current_user``,
    which would swallow the 403 for a deactivated account as well.
    """
    token = extract_token(request)
    if not token:
        return None
    try:
        return await _principal_from_token(token, session)
    except HTTPException:
        return None


def require_role(minimum: str) -> Callable:
    """A dependency asserting the caller holds at least ``minimum``.

    ``roles.satisfies`` is an ordering comparison, so ``admin`` passes a
    ``member`` check without anyone having to remember to list it.
    """
    roles.rank(minimum)  # Fail at import on a typo'd role, not at request time.

    async def dependency(
        principal: Principal = Depends(get_current_user),
    ) -> Principal:
        if not roles.satisfies(principal.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {minimum} role.",
            )
        return principal

    return dependency


#: Any signed-in account, including a visitor. The floor of the ordering
#: rather than "no check at all" -- naming it means an endpoint open to
#: everyone says so, and a future role below `visitor` does not silently gain
#: access to everything using it.
require_visitor = require_role(roles.VISITOR)
require_member = require_role(roles.MEMBER)
require_admin = require_role(roles.ADMIN)


# ---------------------------------------------------------------------------
# Rate-limit guards
#
# These live here rather than in `rate_limit.py` so that module stays free of
# FastAPI: the counting logic is worth testing without an app around it.
# ---------------------------------------------------------------------------


def client_ip(request: Request) -> str:
    """The caller's address, honouring `X-Forwarded-For` when configured.

    The header is trusted only when `TRUST_PROXY_HEADERS` is set, because
    anyone can send it. Trusting it unconditionally would let a caller pick
    their own rate-limit key -- which is to say, opt out of rate limiting --
    and behind no proxy at all there is nothing to gain by reading it.
    """
    if os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


async def enforce(limit: rate_limit.Limit, principal: str) -> None:
    """Count one event, turning an exceeded limit into a 429.

    **The counter gets its own transaction, deliberately.** The request-scoped
    session from `get_session` rolls back when the handler raises -- which is
    exactly what a failed login and a throttled request both do. Counting a
    failed login attempt inside that session would roll the count back with
    it, so the fifth wrong password would be as unthrottled as the first, and
    the limit protecting against credential stuffing would never fire. The
    count must outlive the request that failed.

    `Retry-After` is not decoration either: without it a client that retries
    immediately turns one throttled caller into a busy loop against the
    limiter itself.
    """
    if rate_limit.rate_limiting_disabled():
        return

    from db.engine import get_session_factory

    async with get_session_factory()() as session:
        try:
            await rate_limit.check(session, limit, principal)
        except rate_limit.RateLimitExceeded as exc:
            # Commit before raising: the attempt happened, so it counts.
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def enforce_chat_quota(
    request: Request,
    principal: Principal | None = Depends(get_optional_user),
) -> Principal | None:
    """Chat's limits, for a caller who may not have an account.

    Chat is the public front door -- a prospective client arrives and starts
    asking, with no signup in the way -- so this cannot require a principal.
    When there is one it keys on the account, which is the stronger identity;
    when there is not it keys on the address.

    **The global ceiling is not decoration.** An IP is a weak key: shared
    behind NAT, rotated with a VPN in seconds. The per-caller limits bound an
    ordinary user and a determined one walks straight past them, which on a
    budget of 8,000 tokens a minute against turns costing 6,871 is the
    difference between a busy afternoon and an exhausted quota.

    Checked caller-first so an ordinary user who is simply going too fast is
    told that, rather than being told the whole service is busy.
    """
    subject = str(principal.id) if principal is not None else f"ip:{client_ip(request)}"
    await enforce(rate_limit.CHAT_PER_MINUTE, subject)
    await enforce(rate_limit.CHAT_PER_DAY, subject)
    await enforce(rate_limit.CHAT_GLOBAL_PER_DAY, "all")
    return principal


async def enforce_ingest_quota(
    principal: Principal = Depends(get_current_user),
) -> Principal:
    await enforce(rate_limit.INGEST_PER_MINUTE, str(principal.id))
    return principal

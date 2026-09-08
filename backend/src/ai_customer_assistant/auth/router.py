"""`/auth` — login, refresh, logout, the current user, and account creation.

## Login

Constant-cost by construction. A missing account and a wrong password take
the same path, spend the same Argon2 work (``verify_or_dummy`` hashes against
a dummy when there is no stored hash), and return the same message. An
endpoint that says "no such user" quickly and "wrong password" slowly has
handed over an account-enumeration oracle, and knowing which addresses have
accounts is most of the work of a targeted attack.

## Refresh, and why it rotates

Presenting a refresh token revokes it and issues a new pair. So a token
presented twice is either a race or a theft, and the second presentation is
detectable -- the row is already revoked. This module treats that as
compromise and revokes **every** refresh token for the user, which logs the
thief and the legitimate user out together and forces a password-backed
login. The alternative -- ignoring reuse -- means a stolen token stays
usable for its full fourteen days beside the real one, invisibly.

That is the whole reason refresh tokens are stateful while access tokens are
not. A purely stateless scheme cannot revoke anything, and a logout that only
deletes the client's copy of a token is theatre.

## Account creation

`POST /users` is admin-only, and there is no self-signup: this is an
internal tool, and an open registration endpoint on it would be a way to
mint yourself a `member` account and read the entire corpus. The first admin
comes from `scripts/create_user.py`, which talks to the database directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import passwords, rate_limit, roles, tokens
from auth.cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from auth.dependencies import (
    Principal,
    client_ip,
    enforce,
    get_current_user,
    require_admin,
)
from auth.models import (
    CreateUserRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from db.engine import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# One message for every credential failure. See the module docstring.
_BAD_CREDENTIALS = "Incorrect email or password."


def _access_max_age() -> int:
    return int(tokens.ACCESS_TOKEN_TTL.total_seconds())


def _refresh_max_age() -> int:
    return int(tokens.REFRESH_TOKEN_TTL.total_seconds())


def _user_response(user) -> UserResponse:
    return UserResponse(
        id=user.id, email=user.email, role=user.role, is_active=user.is_active
    )


async def _issue_session(
    user,
    *,
    session: AsyncSession,
    response: Response,
    transport: str,
    user_agent: str | None,
) -> TokenResponse:
    """Mint a token pair, record the refresh token, and deliver both."""
    from db.models import RefreshToken

    access_token, _ = tokens.issue_access_token(user.id, user.role)
    refresh_token, refresh_claims = tokens.issue_refresh_token(user.id, user.role)

    session.add(
        RefreshToken(
            jti=refresh_claims.jti,
            user_id=user.id,
            issued_at=refresh_claims.issued_at,
            expires_at=refresh_claims.expires_at,
            user_agent=(user_agent or "")[:256] or None,
        )
    )

    body = TokenResponse(user=_user_response(user), expires_in=_access_max_age())
    if transport == "bearer":
        return body.model_copy(
            update={"access_token": access_token, "refresh_token": refresh_token}
        )

    set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        access_max_age=_access_max_age(),
        refresh_max_age=_refresh_max_age(),
    )
    return body


async def _revoke_all_for_user(session: AsyncSession, user_id) -> None:
    from db.models import RefreshToken

    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    from db.models import AppUser

    # Both keys, before any work is done. Per-IP stops one host working
    # through a list of addresses; per-email stops a distributed caller
    # working through passwords for one account. Neither alone is enough.
    email = payload.email.lower()
    await enforce(rate_limit.LOGIN_PER_IP, client_ip(request))
    await enforce(rate_limit.LOGIN_PER_EMAIL, email)

    result = await session.execute(
        select(AppUser).where(AppUser.email == email)
    )
    user = result.scalar_one_or_none()

    # Runs even when `user is None`, so the absent-account path costs what
    # the wrong-password path costs.
    stored_hash = user.password_hash if user is not None else None
    if not passwords.verify_or_dummy(stored_hash, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CREDENTIALS
        )

    # Below here the password was correct, so a specific message is safe --
    # the caller already proved they own the account.
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is deactivated.",
        )
    if user.is_service_account:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service accounts cannot sign in interactively.",
        )

    # The one moment the plaintext is legitimately in hand, so the only
    # moment a hash made under weaker parameters can be upgraded.
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(payload.password)

    user.last_login_at = datetime.now(timezone.utc)
    logger.info("Login succeeded for %s (%s).", user.email, user.role)
    return await _issue_session(
        user,
        session=session,
        response=response,
        transport=payload.transport,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    from db.models import AppUser, RefreshToken

    raw = payload.refresh_token or request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token."
        )

    try:
        claims = tokens.decode_token(raw, expected_type=tokens.REFRESH_TYPE)
    except tokens.TokenError as exc:
        logger.debug("Rejected refresh token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is not valid.",
        ) from exc

    stored = await session.get(RefreshToken, claims.jti)
    if stored is None:
        # Correctly signed but unknown: either swept after expiry, or issued
        # by a deployment with a different database. Not necessarily an
        # attack, so it does not trigger the family revocation below.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is no longer valid.",
        )

    if stored.revoked_at is not None:
        # Rotation means this token was already spent. Someone is holding a
        # copy of a token that was used -- revoke the whole family.
        logger.warning(
            "Refresh token reuse for user %s (jti %s); revoking all sessions.",
            stored.user_id,
            stored.jti,
        )
        await _revoke_all_for_user(session, stored.user_id)
        # Commit before raising. `get_session` rolls back when the handler
        # raises, and this handler is about to -- so without an explicit
        # commit the response to a *detected token theft* would be silently
        # undone, leaving the thief's freshly-issued pair working. The 401 is
        # the visible half of this branch; the revocation is the half that
        # matters.
        await session.commit()
        clear_auth_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This session was revoked. Please sign in again.",
        )

    user = await session.get(AppUser, stored.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account can no longer sign in.",
        )

    stored.revoked_at = datetime.now(timezone.utc)
    return await _issue_session(
        user,
        session=session,
        response=response,
        transport=payload.transport,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke the presented refresh token and clear both cookies.

    Deliberately not authenticated with an access token: logging out must
    work when the access token has already expired, which is exactly when a
    user is most likely to click it. Presenting a refresh token is proof
    enough to revoke that same token, and revoking a token is not an action
    an attacker gains anything from.
    """
    from db.models import RefreshToken

    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        try:
            claims = tokens.decode_token(raw, expected_type=tokens.REFRESH_TYPE)
        except tokens.TokenError:
            claims = None
        if claims is not None:
            stored = await session.get(RefreshToken, claims.jti)
            if stored is not None and stored.revoked_at is None:
                stored.revoked_at = datetime.now(timezone.utc)

    # 204 either way: whether a session was there to end is not information
    # this endpoint needs to disclose, and the client's intent is satisfied.
    # Returning None lets FastAPI apply the Set-Cookie headers written onto
    # the injected `response`; constructing a fresh Response here would drop
    # them and leave the browser logged in.
    clear_auth_cookies(response)
    return None


@router.get("/me", response_model=UserResponse)
async def me(principal: Principal = Depends(get_current_user)) -> UserResponse:
    return UserResponse(
        id=principal.id, email=principal.email, role=principal.role, is_active=True
    )


@router.post(
    "/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
async def create_user(
    payload: CreateUserRequest,
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    from db.models import AppUser

    if payload.role not in roles.ALL_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown role {payload.role!r}. Known roles: "
            f"{', '.join(roles.ALL_ROLES)}.",
        )
    try:
        password_hash = passwords.hash_password(payload.password)
    except passwords.WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    user = AppUser(
        email=payload.email.lower(),
        password_hash=password_hash,
        role=payload.role,
        is_active=True,
        is_service_account=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        ) from exc

    logger.info("Account created: %s (%s).", user.email, user.role)
    return _user_response(user)

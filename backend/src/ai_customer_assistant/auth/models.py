"""Request and response shapes for the auth router."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from auth import roles

# How the caller wants its tokens delivered.
#
# `cookie` is the default and what the browser app uses: the tokens are set
# as HttpOnly cookies and do not appear in the response body at all, so no
# script -- including one injected into the page -- can read them.
#
# `bearer` is for scripts and curl, which have no cookie jar worth the name.
# Asking for it explicitly is the point: a token ends up somewhere readable
# only when a caller has said, in the request, that it wants that.
Transport = Literal["cookie", "bearer"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    transport: Transport = "cookie"


class RefreshRequest(BaseModel):
    """The refresh token, for callers that do not use cookies.

    Left unset by the browser, which sends the `refresh_token` cookie
    instead; the endpoint accepts either.
    """

    refresh_token: str | None = None
    transport: Transport = "cookie"


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool


class TokenResponse(BaseModel):
    """The result of a login or a refresh.

    `access_token` and `refresh_token` are populated only for
    `transport="bearer"`. Under the cookie transport they stay `None` and the
    credentials are in `Set-Cookie` headers, where script cannot reach them.

    `expires_in` is sent either way: the frontend needs to know when to
    refresh, and the lifetime of a token is not a secret.
    """

    user: UserResponse
    expires_in: int
    token_type: str = "bearer"
    access_token: str | None = None
    refresh_token: str | None = None


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    role: str = roles.DEFAULT_ROLE

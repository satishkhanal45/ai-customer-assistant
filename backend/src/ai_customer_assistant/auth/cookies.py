"""Cookie transport for the browser.

The browser gets its tokens in cookies, not in a JavaScript-readable store.
That is the decision worth defending, because the reflex answer is
``localStorage``.

**Why cookies here.** The frontend renders model output into the DOM
(``pages/chat.js`` builds HTML from answer text), so the cross-site-scripting
surface is real rather than theoretical. A token in ``localStorage`` is
readable by any script that runs on the page, which puts the session one
prompt-injection away from theft. An ``HttpOnly`` cookie is not readable by
script at all, so the same bug leaks nothing.

**Why this is available at all.** ``main.py`` serves the frontend from the
API's own origin, so a cookie is same-site by construction and
``SameSite=Strict`` costs nothing. That single deployment fact is what makes
cookies the easy choice here and CSRF a non-problem; a cross-origin frontend
would have forced a different design.

``Secure`` defaults to on. Browsers treat ``http://localhost`` as a
trustworthy origin, so local development over plain HTTP still works --
but a deployment reached over plain HTTP at a LAN address must set
``AUTH_COOKIE_SECURE=false`` and understand that it is sending session
tokens in clear text.
"""

from __future__ import annotations

import os
from typing import Final

from fastapi import Response

ACCESS_COOKIE: Final[str] = "access_token"
REFRESH_COOKIE: Final[str] = "refresh_token"

# The refresh cookie is scoped to the only routes that consume it, so it is
# not attached to every chat message and every graph query. A credential
# sent only where it is needed is one that appears in fewer logs.
REFRESH_COOKIE_PATH: Final[str] = "/auth"

SECURE_ENV: Final[str] = "AUTH_COOKIE_SECURE"
SAMESITE_ENV: Final[str] = "AUTH_COOKIE_SAMESITE"


def cookies_are_secure() -> bool:
    return os.environ.get(SECURE_ENV, "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def samesite_policy() -> str:
    value = os.environ.get(SAMESITE_ENV, "strict").strip().lower()
    return value if value in {"strict", "lax", "none"} else "strict"


def set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_max_age: int,
    refresh_max_age: int,
) -> None:
    secure = cookies_are_secure()
    samesite = samesite_policy()
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=access_max_age,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=refresh_max_age,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path=REFRESH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    """Delete both cookies.

    The path must match the one they were set with or the browser keeps the
    cookie and the user stays silently logged in -- a logout that appears to
    work and does not is worse than one that visibly fails.
    """
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)

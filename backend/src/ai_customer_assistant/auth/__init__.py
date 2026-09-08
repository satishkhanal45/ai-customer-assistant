"""Authentication, authorisation and the outbound-request guard (P0-3).

The design this implements is `authentication_implementation.md` at the repo
root. In one paragraph: JWT (HS256) as the credential format, a 15-minute
access token plus a 14-day *rotating* refresh token whose id is stored so it
can actually be revoked, carried to the browser in an `HttpOnly` cookie and
to scripts in an `Authorization: Bearer` header, over Argon2id password
hashes, with two roles (`member` and `admin`).

Layout:

  roles.py         the role ordering, and nothing else
  passwords.py     Argon2id hashing and verification
  tokens.py        issuing and decoding JWTs
  dependencies.py  the FastAPI dependencies that enforce the above
  models.py        request/response shapes
  router.py        /auth/login, /refresh, /logout, /me, /users
  rate_limit.py    per-user and per-IP request limits
  ssrf.py          the outbound-URL guard for the crawler

`ssrf.py` is the odd one out — it defends against a server-side request
forgery rather than authenticating anyone. It lives here because it is part
of the same piece of work and because "the module that decides whether a
request is allowed" is the closest thing to a home it has.

Nothing in this package is imported for its side effects, and no module here
reads the environment at import time: `AUTH_SECRET` is resolved on use, so
importing `auth.tokens` in a test process that has no secret is safe.
"""

# Authentication — Implementation Plan (P0-3)

**Status:** **implemented.** Built across `backend/src/ai_customer_assistant/auth/`,
migration `8e5a3c9d21f7`, and the frontend's `session.js` / `pages/login.js`.
§15 records where the implementation departed from this plan and why.
**Decisions settled:** two roles (`member` / `admin`, §3); `/chat` requires
authentication (§6).
**Date:** 2026-09-06
**Covers:** `status.md` P0-3 — authentication, authorisation, the CORS
wildcard, and the SSRF hole in the crawler.

---

## 1. What is actually exposed today

Measured, not assumed:

- **13 endpoints, zero authentication dependencies.** No `Depends(...)` that
  checks anything, no `HTTPBearer`, no session middleware, nowhere.
- `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]`.
- `_uploaded_by()` in `api/ingest.py` returns the hardcoded service-account
  UUID for **every** ingestion, so nothing is attributable to a person.

Anyone who can reach the port can run LLM-billed chat turns on your Groq
quota, upload documents into the knowledge base, read the entire knowledge
graph, create tickets that send email, and — the sharpest edge —

```
POST /ingest/crawl  {"url": "http://169.254.169.254/latest/meta-data/"}
```

make the server fetch any URL they name, including cloud metadata endpoints
and anything else reachable from inside your network. That is server-side
request forgery, and it is exploitable today by an unauthenticated caller.

---

## 2. Three facts about this codebase that shape the design

These are why the plan below is not the generic "put JWTs in localStorage"
answer.

**The frontend is served same-origin.** `main.py` mounts it with
`app.mount("/", StaticFiles(...))`, and `frontend/src/config.js` resolves
`apiBase` to `location.origin`. The browser app and the API are the same
origin, which means cookies work naturally and **CORS is not needed for the
application at all**. The wildcard is not enabling anything; it is pure
exposure.

**`pyjwt` and `argon2-cffi` are already in `uv.lock`** — pulled in
transitively, not declared. So the cryptographic pieces need no new
dependency decisions, but they *do* need adding to `pyproject.toml`
explicitly. Relying on a transitive dependency for authentication is how you
get a security feature silently removed by an unrelated upgrade.

**`AppUser` already exists** (`db/models.py`) with `id`, `email`,
`is_service_account`, `created_at`, and a seeded service account. It has **no
password, no role, no active flag** — so one additive migration, not a new
table.

---

## 3. The technique, and why

### Credential format: JWT (HS256), short-lived access + refresh

| Token | Lifetime | Carries |
|---|---|---|
| Access | 15 minutes | `sub` (user id), `role`, `exp`, `iat`, `jti` |
| Refresh | 14 days | `sub`, `exp`, `jti`, and a server-side row so it can be revoked |

Stateless access tokens keep *verification* to a signature check with no
database round trip. The shipped dependency then loads the user row anyway,
to check `is_active` and to read the role from the row rather than from the
token — see §15.1 for why that trade came out the other way round. Refresh tokens are stateful precisely so logout and revocation
actually work; a purely stateless scheme cannot revoke anything, and
"logout" that only deletes the client's copy is theatre.

`HS256` with a single secret is right for one backend service. `RS256` earns
its keys only when a separate service must verify without being able to
mint, which is not the case here.

### Transport: HttpOnly cookie for the browser, `Authorization: Bearer` for scripts

This is the decision worth arguing for, because the common answer is wrong
for this application.

**The browser gets an `HttpOnly; Secure; SameSite=Strict` cookie.** The token
is never readable from JavaScript, so an XSS bug cannot exfiltrate it. Given
the frontend renders LLM output into the DOM — `renderAssistant()` builds
HTML from model text — the XSS surface is real and not hypothetical.
Storing a token in `localStorage` would put the session one prompt-injection
away from theft.

`SameSite=Strict` is available *because* the app is same-origin, and it
removes the CSRF problem almost entirely. Same-origin is doing real security
work here, not just convenience.

**Scripts and `curl` send `Authorization: Bearer <jwt>`.** Same token format,
same verifier, different transport. One implementation, two doors.

The dependency resolves in that order: cookie first, header second.

### Passwords: Argon2id

Already in the lockfile. Memory-hard, the current default recommendation,
and no reason to reach for bcrypt. Parameters: `time_cost=2`,
`memory_cost=64 MiB`, `parallelism=2` — tuned so a login takes roughly
50–100 ms on this hardware, which is slow enough to matter for an attacker
and invisible to a user.

Login is rate-limited per IP *and* per email, and returns an identical
response for "no such user" and "wrong password", including timing — verify
against a dummy hash when the user does not exist, so absence and rejection
cost the same.

### Roles: two — `member` and `admin`

`member` → `admin`, checked by a dependency factory `require_role(minimum)`.
No permission matrix, no groups, no per-object rules.

The line is **"uses the system" vs. "changes the system"**:

- **`member`** — everything the application currently does. Ask questions,
  browse the knowledge graph, upload documents, run crawls, watch jobs.
  Anyone with an account is a trusted colleague who can add content.
- **`admin`** — all of that, plus the system's own controls: who has
  accounts, what the prompts and retrieval parameters are, and destroying
  sources.

Across the 13 endpoints that exist **today**, `member` and `admin` are
identical; the only live difference on day one is account creation.
Everything the split buys is in the unbuilt `/admin/*` rows of §5 — prompt
management, retrieval configuration, source deletion. Those are the
operations where a mistake is *silent*: change `similarity_threshold` and
every answer degrades with no error anywhere. A member's worst case is a bad
PDF, which is visible, attributable and deletable. Keeping that boundary
named now is what stops those endpoints from landing on the same gate as
uploading a file, which is where they go if the only non-member role is the
one you are already logged in as.

An earlier draft of this document proposed a third tier (`viewer` /
`editor` / `admin`) separating read-only access from the ability to add
content. It was dropped deliberately: it only pays off when the group who
may *read* the corpus is meaningfully larger than the group who may *write*
to it, which is not the shape of this team. If that changes, see the note on
storage below — reinstating it is one enum entry.

**Store the role as an ordered value, not a boolean.** `role VARCHAR(16)`
compared against a rank map, never `is_admin BOOLEAN`. The two cost exactly
the same today, but inserting a middle tier later costs one enum entry and a
reclassification of a few endpoints, rather than a migration plus a rewrite
of every call site plus a re-audit.

---

## 4. What gets built

```
backend/src/ai_customer_assistant/auth/
    __init__.py
    tokens.py        issue / verify / decode JWTs; pure, no I/O
    passwords.py     Argon2 hash + verify; pure
    dependencies.py  get_current_user, require_role, optional_user
    models.py        pydantic request/response shapes
    router.py        POST /auth/login | /refresh | /logout, GET /auth/me
    ssrf.py          URL safety check for the crawler
```

Plus:

- **One migration.** Adds to `app_user`: `password_hash TEXT NULL`,
  `role VARCHAR(16) NOT NULL DEFAULT 'member'`, `is_active BOOLEAN NOT NULL
  DEFAULT true`, `last_login_at TIMESTAMPTZ NULL`. Creates `refresh_token`
  (`jti` PK, `user_id` FK, `expires_at`, `revoked_at`, `user_agent`).
  Additive; the existing service-account row keeps working with a null
  password because service accounts do not log in.
- **`AUTH_SECRET`** in the environment, with **no default**. The process must
  refuse to start without it — the same boot-time-failure discipline
  `timeouts.py` already uses, and for the same reason: a silently-defaulted
  signing key is worse than a crash.
- **A CLI** (`scripts/create_user.py`) to create the first admin, and
  **`POST /auth/users`** (admin-only) for every account after it. The endpoint
  matters beyond convenience: it gives the `admin` tier a live,
  test-exercised caller from the first commit, so the check is not dead code
  waiting to be trusted for the first time six months from now.

  > **Reversed 2026-09-13.** This plan said "self-signup is not appropriate
  > for an internal knowledge tool", and that position was overruled
  > deliberately: **`POST /auth/signup`** is public and creates `member`
  > accounts from the login page. The reasoning against it still holds and is
  > recorded in the README's known limitations — a member reads the whole
  > knowledge graph, writes to it, and spends the shared model budget, so on a
  > reachable deployment the corpus is effectively public.
  >
  > What makes the endpoint safe *as an endpoint*, as opposed to safe as a
  > policy: the role is hardcoded rather than taken from the request
  > (`SignupRequest` has no `role` field, and a test pins that an
  > `admin` in the body is ignored), and it is rate limited to 3 per IP per
  > hour. `POST /auth/users` keeps its role parameter precisely because the
  > caller there is already an authenticated admin.
  >
  > Admin accounts remain CLI- or admin-created only. If the policy is ever
  > revisited, the gate belongs on this same endpoint: an email-domain
  > allowlist or an invite code, not a second route.
  >
  > **Reverted 2026-09-13, and this document's original position restored.**
  > `POST /auth/signup` is gone. Chat became public instead, so nobody needs
  > an account to use the assistant and self-signup has nothing left to do.
  > The original text — "self-signup is not appropriate for an internal
  > knowledge tool" — turned out to be right for a reason it did not state:
  > not that strangers should be kept out, but that an account they can
  > create in ten seconds never kept anyone out. What bounds a public
  > endpoint is a quota, not a credential. See
  > `auth.rate_limit.CHAT_GLOBAL_PER_DAY`.
  >
  > **Amended the same day.** Signup creates a **`visitor`**, a third role
  > below `member`, not a member. This document's two-role model assumed
  > everyone with an account was a trusted colleague; once signup is public
  > that stops being true, and the fix is a tier for the untrusted population
  > rather than widening what the trusted one means. `member` keeps the
  > meaning described in §4 — a colleague who adds content — and is now
  > admin-created only. See `auth/roles.py`.

---

## 5. Endpoint classification

All 13 endpoints that exist today, plus the ones §13 and `status.md` leave
unbuilt. Nothing is implicitly protected: every route is either on the public
allowlist — `GET /health`, `POST /auth/login`, `POST /auth/refresh`, and
nothing else — or it requires a role.

| Endpoint | Today | Anonymous | `member` | `admin` |
|---|---|:---:|:---:|:---:|
| `GET /health` | open | ✅ | ✅ | ✅ |
| `POST /auth/login` · `/auth/refresh` | — | ✅ | ✅ | ✅ |
| `GET /auth/me` · `POST /auth/logout` | — | — | ✅ | ✅ |
| `POST /chat` · `POST /chat/stream` | open | — | ✅ | ✅ |
| `GET /graph/search` | open | — | ✅ | ✅ |
| `GET /graph/search_value` | open | — | ✅ | ✅ |
| `GET /graph/entities/{id}` | open | — | ✅ | ✅ |
| `GET /graph/entities/{id}/neighbors` | open | — | ✅ | ✅ |
| `GET /graph/subgraph` | open | — | ✅ | ✅ |
| `GET /graph/path` | open | — | ✅ | ✅ |
| `GET /ingest/jobs/{id}` | open | — | ✅ | ✅ |
| `POST /ingest/upload` | open | — | ✅ | ✅ |
| `POST /ingest/crawl` † | open | — | ✅ | ✅ |
| `POST /ingest/crawl/discover` † | open | — | ✅ | ✅ |
| `POST /ingest/crawl/{id}/confirm` † | open | — | ✅ | ✅ |
| `POST /auth/users` (create account) | — | — | — | ✅ |
| `GET /admin/knowledge-sources` | — | — | — | ✅ |
| `GET /admin/jobs` | — | — | — | ✅ |
| `GET /admin/stats` | — | — | — | ✅ |
| `GET /admin/tickets` | — | — | — | ✅ |
| *future* `/admin/prompts/*` | — | — | — | ✅ |
| *future* `/admin/config/*` (model, retrieval params) | — | — | — | ✅ |
| *future* `/admin/users/*` (role change, deactivate) | — | — | — | ✅ |
| *future* `DELETE /admin/sources/{id}` | — | — | — | ✅ |

† also passes through the SSRF guard of §8, which is independent of the
caller's role.

Three of these are worth justifying, because each one is somewhere a
reasonable reader would have drawn the line differently:

**`/graph/*` is not public, even though it is read-only.** Those six
endpoints walk the entire internal knowledge graph — every entity, attribute
and relationship extracted from Alpinist Studios' own documents. Read-only
means it does not write, not that it is harmless: it is a far more efficient
way to exfiltrate the corpus than asking the chatbot five hundred questions.

**`GET /ingest/jobs/{id}` is a member endpoint rather than a public one.** It
is a read, and it leaks source filenames and raw error text.

**`crawl/discover` is gated even though it is nominally a preview.** It makes
the server fetch a URL. That is the SSRF primitive whether or not the result
is kept — the request has already left your network by the time anyone
decides not to store it.

---

## 6. `/chat` is authenticated — settled

`POST /chat` and `POST /chat/stream` require `member`. This is the decision
that shapes the rest of the plan, so it is worth recording what it commits
to and what it rules out.

**What it means.** This is an internal support tool, not a public widget.
The knowledge base holds Alpinist Studios' own documents, tickets collect an
email address for follow-up, and `status.md` describes the goal as "a
deployable internal product". Every turn is attributable to a person, and
the population that can spend the Groq quota is exactly the user list.

**What it simplifies.** Four things stop being hard problems:

- **Rate limiting becomes per-user** rather than per-IP. Per-IP limiting is
  a permanent approximation — it punishes offices behind one NAT and does
  nothing against a botnet. Per-user is exact.
- **The token budget is bounded by the user list**, not by the internet.
  This is not theoretical: the Groq free tier's daily budget was exhausted
  repeatedly during this project's own testing, by one developer.
- **Ticket emails become attributable.** The ticket agent sends mail on
  behalf of whoever asked; today that is anonymous.
- **The public allowlist shrinks to three routes** — `/health` and the two
  unauthenticated auth endpoints. A three-item allowlist is one a reviewer
  can hold in their head, and it is what makes the exhaustive route test in
  §12 a meaningful check rather than a long table of exceptions.

**What it rules out, and what to do if that changes.** There is no
customer-facing chat surface. If one is wanted later, the right shape is a
*separate* endpoint — `POST /chat/public` — with its own conversation-token
scheme and a per-IP daily token cap, not a relaxation of this one. Keeping
them apart means the public surface never inherits the internal corpus by
default: it would need its own retrieval scope, which is a decision that
should be made explicitly rather than by an authentication flag.

**One consequence to design for.** With `/chat` authenticated, the login
page is the first thing a user meets, and an expired token mid-stream is a
visible failure. §7 covers the 401-refresh-replay path, and it matters more
here than it would for a REST-only API: `/chat/stream` can be held open for
tens of seconds against a 15-minute access token, so a turn that starts
valid can finish invalid. The refresh must happen on the *next* request, not
mid-stream, and the stream's existing `error` terminal event is what surfaces
it.

---

## 7. Request flow

### Today

```
Browser ──────────────► FastAPI ──► router ──► ChatService / ingest / graph
   (no credential)      (CORS: *)   (no check)
```

Anyone reaching the port reaches everything.

### After

```
POST /auth/login {email, password}
        │
        ├─ Argon2 verify (dummy-hash on miss, constant cost)
        ├─ issue access JWT (15 min) + refresh JWT (14 d, row in refresh_token)
        └─► Set-Cookie: access_token=…; HttpOnly; Secure; SameSite=Strict
            Set-Cookie: refresh_token=…; HttpOnly; Secure; SameSite=Strict; Path=/auth

Any protected request
        │
   ┌────▼─────────────────────────────────────────────┐
   │ get_current_user                                  │
   │   1. read cookie, else Authorization: Bearer      │
   │   2. verify signature + exp   ── fail ──► 401     │
   │   3. load AppUser             ── inactive ─► 403  │
   └────┬─────────────────────────────────────────────┘
        │ request.state.user
   ┌────▼─────────────┐
   │ require_role(...)│ ── insufficient ──► 403
   └────┬─────────────┘
        ▼
     the handler, which now knows who is calling
        │
        └─► /ingest/*: uploaded_by = user.id   (not the service account)
        └─► /chat:     rate limit keyed on user.id
        └─► /crawl:    URL passes the SSRF guard before any fetch

Access token expires (15 min)
        │
        └─► 401 with WWW-Authenticate → frontend silently POSTs /auth/refresh
            → rotate: old jti revoked, new pair issued → retry the request once
```

The refresh **rotates** and revokes the old `jti`. If a revoked refresh token
is ever presented, that is evidence of theft: revoke the whole family and
force a re-login.

### What the frontend needs

Smaller than it looks, because cookies ride along automatically:

1. `fetch(..., {credentials: 'same-origin'})` in `api.js` — one line.
2. A 401 interceptor: try `/auth/refresh` once, replay, else route to login.
3. A login page and a route guard in the hash router.
4. `/chat/stream` needs the same treatment — an SSE stream that 401s
   mid-turn should surface as the existing `error` terminal event rather
   than a dead connection. The stream already has that event type, so this
   is wiring, not new protocol.

**No token handling in JavaScript at all**, which is the point of the cookie
transport.

---

## 8. The SSRF guard

Independent of authentication, and worth doing even if `/chat` stays public —
authentication reduces *who* can trigger it, not what it does.

As built, in `auth/ssrf.py`:

```python
async def assert_url_is_safe(url: str) -> SafeTarget:
    # 1. scheme in {http, https}          — no file://, gopher://, ftp://
    # 2. optional CRAWL_DOMAIN_ALLOWLIST — if set, the host must match
    # 3. resolve the hostname to every address it has
    # 4. reject if ANY of them is loopback, link-local, unspecified,
    #    multicast, reserved, private — or simply not globally routable
    # 5. after the response, re-check the address actually connected to
    # 6. re-run all of it per redirect hop; cap redirects at 3
```

**Step 4's last clause was added during implementation and is doing real
work.** On Python 3.12, `100.64.0.0/10` — carrier-grade NAT, which reaches
ISP infrastructure — reports `is_private=False` *and* `is_global=False`, so
an enumeration of the named flags lets it through. A test caught it. "Must
be globally routable" is the property that actually matters and does not
need maintaining as new special-use ranges are assigned.

**Step 5 is weaker than this plan promised, and that is worth being explicit
about.** The complete fix for DNS rebinding — resolve, then connect to the
address that was validated, carrying the hostname only in the `Host` header
and TLS SNI — has no supported form in `httpx`, and the workaround
(requesting `https://<ip>/` with an overridden `Host`) breaks certificate
verification, trading one hole for another. What ships instead reads the
peer address off the completed connection and rejects the response before a
byte of it is used. An attacker can still cause one blind request to an
internal address; they cannot see the answer. Combined with `member`
authentication on the endpoint, that is defensible — and it is written down
in the module docstring rather than left for someone to discover.

**Step 6 applies in two places, not one.** Guarding only the API endpoint
would miss the crawler entirely: a site is crawled by *following its links*,
and a link on a public page pointing at an intranet host is fetched by the
same code path. So the guard also sits in `ingestion/crawler/fetcher.py`,
which is the single network I/O boundary for page and document fetching —
covering discovery, `robots.txt`, `sitemap.xml`, document downloads, and
every followed link. A refusal there raises `BlockedURLError`, a subclass of
`FetchError`, so one bad link fails that document rather than the crawl.

**`CRAWL_ALLOW_ADDRESSES`** exempts named CIDRs, for a deployment that
genuinely crawls an intranet and for the crawler's own fixture site on
loopback. It is a list of networks rather than a boolean precisely so that
opening `10.1.0.0/16` does not also open `169.254.169.254`.

---

## 9. Rate limiting

Because §6 settled `/chat` as authenticated, every limit except the login
one is keyed on the **authenticated user id**, not the client IP. That is
the accurate key: per-IP limiting punishes an office behind one NAT and does
nothing against a distributed caller. A `slowapi`-style middleware backed by
the database is enough (Redis if one is ever added):

| Scope | Key | Limit | Reason |
|---|---|---|---|
| `POST /auth/login` | IP **and** email | 5 / 15 min | Credential stuffing — the one limit that must work before anyone is identified |
| `POST /chat` · `/chat/stream` | user id | 20 / min, 500 / day | Every turn costs Groq tokens |
| `POST /ingest/*` | user id | 10 / min | Crawls are expensive and noisy |
| ~~all routes~~ | ~~IP~~ | — | **Dropped during implementation.** See below. |

The chat limit matters more here than in most applications: the Groq free
tier's daily token budget was exhausted repeatedly during this project's own
testing, by one developer. Authentication bounds who can spend it; the
per-user daily cap bounds how much any one of them can.

The all-routes backstop was dropped. It would have cost a database write on
every request — including every stylesheet and every icon served by
`StaticFiles` — to defend a surface that, once §6's decision landed, consists
of three routes, one of which (`/auth/login`) already carries the strictest
limit in the table. The cost was certain and the benefit was not.

The login limit still has to work for callers who have not been identified
yet, so the limiter keys on both a user id and a client address even though
only the login rows use the second.

**One implementation detail turned out to matter more than the numbers.** The
counter runs in *its own transaction*, not the request's. `get_session` rolls
back when a handler raises — and a failed login and a throttled request are
both handlers that raise. Counting inside that session would have rolled the
count back with it, so the fifth wrong password would have been as
unthrottled as the first and the limit protecting against credential stuffing
would never have fired at all.

---

## 10. What this breaks, and the migration path

Honest inventory:

| Thing | Impact | Handling |
|---|---|---|
| `scripts/crawl_and_ingest.py` | **None** — talks to Postgres directly, not the API | — |
| `scripts/run_worker.py` | **None** — same | — |
| The 640-test suite | `tests/api/*` will 401 | An `authenticated_client` fixture minting a test token |
| The browser UI | Needs login before ingest/graph pages | §7 |
| `curl` examples in `README.md` | Need a token | Update the docs alongside |
| Existing `knowledge_source.uploaded_by` rows | All point at the service account | Leave them; historical attribution cannot be invented |

Nothing in the ingestion pipeline touches the API, which makes this far less
invasive than it first appears.

---

## 11. Staging

Each stage is independently shippable and independently valuable.

**Stage 1 — close the holes that need no auth model (about half a day).**
SSRF guard on the crawler. `allow_origins` from an env allowlist, defaulting
to same-origin only. Rate limiting on `/chat*` and `/ingest/*`, keyed on IP
for now because no identity exists yet — Stage 3 re-keys it to the user id.
This removes the SSRF and the quota-drain risk before any of the identity
work lands, and is the highest value per hour in the whole plan.

**Stage 2 — identity (about a day).** Migration, `auth/` package, login /
refresh / logout / me, the admin-creation CLI, `AUTH_SECRET` required at
boot.

**Stage 3 — enforcement (half a day).** `require_role("member")` across
`/chat*`, `/graph/*` and `/ingest/*`, with the admin-only routes of §5
reserved for when they exist. `uploaded_by` from the caller. Rate-limit keys
switched from IP to user id.

**Stage 4 — the frontend (about a day).** Login page, route guard,
`credentials: 'same-origin'`, the 401-refresh-replay interceptor, SSE
handling.

**Roughly three days**, and Stage 1 alone removes the exploitable part.

---

## 12. How it gets tested

Following the pattern the rest of this codebase now uses — assert the
property, not the implementation:

- **Tokens:** expired rejected, tampered signature rejected, `alg: none`
  rejected, a refresh token rejected where an access token is required.
- **Passwords:** verification succeeds, a wrong password fails, timing is
  indistinguishable between "no such user" and "wrong password".
- **Enforcement, exhaustively:** a parametrised test over *every* route in
  the app asserting each one is either on an explicit public allowlist —
  which §6 pinned at exactly `/health`, `/auth/login` and `/auth/refresh` —
  or returns 401 without a credential. The allowlist is a literal in the
  test, so widening it is a visible diff rather than an omission. That is the test that catches the
  endpoint someone adds next year and forgets to protect — the same shape as
  the ladder-sum check in `timeouts.py`, which exists because checking each
  rung individually missed the real problem.
- **SSRF:** a table of hostile URLs — `169.254.169.254`, `127.0.0.1`,
  `10.0.0.1`, `[::1]`, `localhost`, a DNS name resolving to a private IP, a
  redirect chain ending at one — each asserted rejected.
- **CORS:** no wildcard reaches production config.
- **Live:** the checks that matter run against the container, as every fix in
  `test.md` was verified.

---

## 13. Deliberately not included

- **OAuth / SSO.** Correct eventually, disproportionate for an internal tool
  with no identity provider yet chosen. The `auth/` boundary above keeps it a
  swap rather than a rewrite.
- **Multi-tenancy.** No tenant concept exists anywhere in the schema. Adding
  one is a data-model change, not an auth change.
- **Field-level permissions, groups, per-object ACLs.** Two roles are
  enough for 13 endpoints, and §3 explains how a third is added if that stops
  being true.
- **Password reset by email.** SMTP exists (the ticket flow uses it), so this
  is straightforward, but it is a feature rather than a security fix and can
  follow.

---

## 14. Summary

| | |
|---|---|
| Format | JWT HS256, 15-minute access + 14-day rotating refresh |
| Browser transport | `HttpOnly; Secure; SameSite=Strict` cookie — no token in JS |
| Programmatic transport | `Authorization: Bearer`, same verifier |
| Passwords | Argon2id, already in the lockfile |
| Roles | `member` / `admin`, one ordering check — "uses the system" vs. "changes the system" |
| New dependencies | None — but `pyjwt` and `argon2-cffi` must be *declared* |
| Schema change | One additive migration |
| Effort | ~3 days, in four independently shippable stages |
| `/chat` | Authenticated, `member` — no public surface (§6) |
| Public routes | Exactly three: `/health`, `/auth/login`, `/auth/refresh` |

The single most valuable hour is Stage 1: the SSRF guard and the CORS
allowlist close the exploitable hole and need no decision about users at all.

---

## 15. What changed between the plan and the code

Five places where building it produced a different answer than designing it
did. Recorded because a plan that quietly diverges from its implementation is
worse than no plan.

### 15.1 `get_current_user` reads the database

§3 argued for statelessness on the hot path. The shipped dependency verifies
the signature *and* loads the user row. Two reasons the trade came out the
other way:

- **Deactivation has to take effect now.** A stateless check means an
  offboarded account keeps working until its token happens to expire — up to
  fifteen minutes after someone has been removed.
- **The role is read from the row, not the token.** A demotion applies
  immediately for the same reason. There is a test for it: the same validly
  signed token that says `role=admin` gets a 403 once the row says `member`.

The cost is one indexed primary-key lookup against a turn with a 52-second
budget and four LLM calls in it. It is not measurable.

### 15.2 The login response does not contain a token by default

The plan said "JWT in a cookie for browsers, `Authorization: Bearer` for
scripts" without saying what the *login response body* contains. Returning
the token in both places would have made the `HttpOnly` cookie pointless for
anyone who copied the obvious code path into `localStorage`.

So the transport is a field on the request: `transport: "cookie"` (the
default) sets two cookies and leaves `access_token` null in the body;
`transport: "bearer"` returns the tokens and sets no cookies. A token ends up
somewhere script can read it only when a caller has explicitly asked for
that.

### 15.3 The refresh client has to coalesce

Not in the plan at all, and it would have been a serious bug. Refresh tokens
rotate, and re-presenting a rotated token is treated as theft — the server
revokes every session the user has.

A page that fires four requests in parallel gets four simultaneous 401s. Four
independent refreshes would present the same refresh token four times, and
three of those would look exactly like a stolen token being replayed. **An
ordinary parallel page load would have logged the user out and recorded it as
an attack.** `api.js` keeps one in-flight refresh promise that every waiting
request shares.

### 15.4 Revocation-on-reuse needs an explicit commit

Found by a test that asserted the *consequence* rather than the response
code. On detecting a replayed refresh token the handler revokes every session
for that user and then raises a 401 — and `get_session` rolls back when a
handler raises, which silently undid the revocation. The 401 is the visible
half of that branch; the revocation is the half that matters, and it was
being thrown away.

The same shape bit the rate limiter (§9) independently. Both now commit
before raising. It is worth stating as a rule: **any security-relevant write
that happens on the way to an error response needs its own commit**, because
the framework's default is to discard exactly the work you most wanted kept.

### 15.5 The route-protection test had to walk nested routers

`test_route_protection.py` enumerates the assembled app and asserts every
route is either on a four-entry public allowlist or answers 401. The first
version found one route, because FastAPI 0.141 does not flatten included
routers into `app.routes` — it wraps each one in an object exposing
`original_router`.

That version passed. It reported a completely unprotected API as fully
protected, which is the exact failure mode an exhaustive test is supposed to
eliminate. It was caught only by a companion assertion that the walk finds at
least fourteen routes including `/chat` and `/ingest/upload` — a guard on the
guard. Any test that enumerates something should assert that the enumeration
is not empty; otherwise its passing means nothing.

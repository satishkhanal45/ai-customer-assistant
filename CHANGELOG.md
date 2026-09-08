# Changelog

Notable changes to the AI Customer Assistant. Fuller detail, with the evidence
behind each entry, lives in [`status.md`](status.md) and [`test.md`](test.md).

Entries are grouped by the identifiers those documents use: `P0`/`P1`/`P2` for
problems found by reading the codebase, and `F1`–`F9` for defects found by
running the assembled system against a browser.

## Unreleased — 2026-09-06

The test suite went from **268 passing with 10 failures and 6 errors** to
**759 passing with none**, and the backend grew from ~14.8k to ~18k lines
while the tests more than doubled.

### Fixed — correctness

- **P0-1 — the ticket flow was broken end to end.** Four defects: an
  async/sync split that made every `invoke()` caller raise, an injected
  session factory that was silently ignored, an email subject missing its
  `f` prefix so customers received literal `{ticket_id}`, and a clarifying
  answer that was collected and then discarded. SMTP also moved off the
  event loop.
- **P0-2 — `load_dotenv()` at module import.** Importing a ticket module
  injected every secret in `backend/.env` into the process. Configuration is
  now an entry point's job (`config.load_env`). This also repaired three
  `skipif` guards that had never worked, because they tested for the very
  variables the import was setting.
- **P1-6 — a structured-only lookup that found nothing never fell back.** A
  confident extraction naming an entity with no rows ended retrieval with no
  results, while the semantic index held the answer. The defect was in the
  graph topology, so every unit test passed.
- **F9 — a structured fact deleted the document holding the answer.**
  Deduplication dropped any chunk containing both a fact's value and its
  entity label, as "redundant prose". For a question about a rebrand that
  discarded the entire press release — because the fact recorded the rebrand
  and the document necessarily named both companies. Also fixed: a claimed
  order-preservation property that did not hold.

### Fixed — answer quality

- **P2-2 — retrieval was untuned.** The BGE query instruction was missing
  (queries were embedded under a different convention than the model was
  trained for), and `similarity_threshold` was 0.70 with nothing behind it.
  Measured against the live corpus with a new golden set: the instruction
  moved recall@1 from 76.9% to 92.3%, and the old threshold was rejecting the
  correct source for 21 of 26 answerable questions. Threshold now 0.50, with
  a relative score margin doing the discriminating.
- **P2-1 — two forked ontologies had drifted.** Replaced by one shared
  vocabulary, with the deliberate fuzzy-vs-exact asymmetry between reading
  and writing preserved and test-enforced.
- **F4 — unfiltered structured facts crowded out the answer.** An unslotted
  lookup returned everything known about an entity, and that dump led the
  prompt. Now filtered against the question.
- **F5 — the same question answered differently on different runs.** Routing
  keyed on the shape *and* the confidence of a non-deterministic extraction.
  It now asks one question — is there an entity to look up? — and semantic
  search always runs alongside.
- **F6 — the assistant refused questions the corpus answered.** Scope came
  from a hand-written paragraph unconnected to the knowledge base. It is now
  derived from the live document titles and refreshed on a TTL.

### Fixed — performance and scale

- **P1-1 — vector search did not use pgvector.** Ranking is pushed into the
  database; embeddings no longer cross the wire. HNSW index added.
- **P1-2 / P1-3 / P1-4 / P1-5** — LLM calls moved off the event loop; the
  `?`-splitting that shredded any message containing a question mark removed;
  four independent database engines consolidated into one pooled engine;
  ingestion moved out of the API into a worker service with a stale-job
  reaper.
- **P2-5 — per-process state that broke horizontal scaling.** Ticket
  idempotency and crawl-discovery review moved into the database, where a
  second instance or a restart can see them.
- **F8 — `hybrid_retrieve` removed.** It took a single database session while
  running both arms concurrently, so the strategy it was named after raised.
  Fixed with a session factory, then deleted outright: it had no production
  callers and duplicated orchestration the compiled graph already implements.
  Retrieval behaviour is unchanged — hybrid search lives in the graph's
  fan-out edge, not in that function.
- **F1 / F2 — the timeout ladder.** It bounded one node rather than a whole
  turn, so a request could run to ~76 s under a 60 s client budget; and one
  15 s ceiling for every LLM call was cutting off healthy answer generation.
  The turn is now the budgeted unit, and the budgets are per stage.
- **F7 — every turn was a blank spinner.** `POST /chat/stream` reports
  progress from ~50 ms; `POST /chat` is unchanged as the fallback.

### Fixed — security and privacy

- **P0-3 — the API was entirely open.** All 13 endpoints were anonymous,
  `CORS` allowed every origin, and `POST /ingest/crawl` would fetch any URL a
  caller named — including `http://169.254.169.254/`, which is a
  server-side request forgery yielding cloud credentials in one request.
  Nothing was attributable: `_uploaded_by()` hardcoded the service account.

  Now: JWT (HS256), a 15-minute access token and a 14-day **rotating**
  refresh token whose id is stored so it can be revoked; `HttpOnly; Secure;
  SameSite=Strict` cookies for the browser and `Authorization: Bearer` for
  scripts, through one verifier; Argon2id passwords; two roles, `member` and
  `admin`. `/chat` is authenticated — this is an internal tool, and every
  turn spends Groq tokens against a shared daily budget. The public surface
  is exactly `/health`, `/auth/login` and `/auth/refresh`, and a test
  enumerates the assembled app to assert that every other route refuses an
  anonymous caller.

  The SSRF guard sits at the crawler's fetch boundary rather than only at the
  API, because a site is crawled by following its links: a link on a public
  page pointing at an intranet host went through the same code path.
  Redirects are now walked one hop at a time — `follow_redirects=True` was
  the bypass. Rate limiting is per authenticated user, counted in the
  database rather than in a process dict, which is the defect P2-5 already
  fixed twice elsewhere.

  Two security-relevant writes needed explicit commits, both found by tests
  asserting the consequence rather than the status code: revoking every
  session on detecting a replayed refresh token, and incrementing the login
  counter. Both happen on the way to an error response, and `get_session`
  rolls back when a handler raises — so the response to a *detected token
  theft* was being silently undone, and the fifth wrong password was as
  unthrottled as the first.

  Migration `8e5a3c9d21f7`, additive: four columns on `app_user`, plus
  `refresh_token` and `rate_limit_bucket`. Design and the five places the
  implementation departed from it: `authentication_implementation.md`.
- **P0-4 — full conversation state printed to stdout.** Every node entry and
  exit printed the customer's message, the whole history, and their email
  address, unconditionally, in the Docker image. Replaced by a `DEBUG`-level
  tracer that redacts free text to a shape summary; `LOG_PII=true` is the
  deliberate opt-in.

### Fixed — operability

- **F3 — failures were swallowed with no logging.** The entire diagnostic for
  a failed turn was `"error": "error"`. Both swallow sites now log with a
  traceback, and a provider rate limit is told apart from a defect and worded
  as a wait rather than a breakage.
- **Application `INFO` logging never appeared at all** — uvicorn leaves the
  root logger at WARNING, so every `logger.info` in the codebase was
  discarded. `logging_config.configure_logging()` now runs at startup.
- **P2-3 — a failed ingestion committed the rows it had staged.**
- **P2-4 / P2-6** — the timeout ladder was introduced; dead files, a tracked
  AI-assistant note, a committed crawl artefact and committed debugging
  values were removed, and the README was written.

### Added

- `POST /chat/stream` — server-sent events with progress stages and
  heartbeats.
- `scripts/calibrate_retrieval.py` and `tests/data/golden_retrieval.json` —
  measure recall and the score distribution against the live corpus, so
  retrieval thresholds are set from data rather than intuition.
- `timeouts.py` — the whole request timeout ladder in one place, validated at
  import; the process refuses to start on an inconsistent one.
- Migration `3d6f8b2c17ae` — `ticket.idempotency_key` with a unique
  constraint, and the `crawl_discovery` table.

### Added — admin API

- `GET /admin/knowledge-sources`, `/admin/jobs`, `/admin/stats`,
  `/admin/tickets` (`api/admin.py`), admin-only. The frontend's Admin page
  had shown "Endpoint not available yet" on all four tabs since it was
  written; the data was always in the database. Each returns
  `{ <list>, total, limit, offset }` rather than a bare array, so a caller
  can tell "all of it" from "the first page of it".
- `/admin/stats` also backs the Overview figures, which previously showed a
  dash for two of four cards and reported a capped `/graph/search` result
  as if it were an entity total.
- `ingestion/storage/api.py` stays unregistered, now as a decision rather
  than a gap: it is an upload path duplicating `POST /ingest/upload`, not
  the read surface the page needed.

### Added — authentication

- `auth/` — `tokens.py`, `passwords.py`, `roles.py`, `cookies.py`,
  `dependencies.py`, `models.py`, `router.py`, `rate_limit.py`, `ssrf.py`.
- `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me`,
  `POST /auth/users` (admin only).
- `scripts/create_user.py` — bootstraps the first admin; there is no
  self-signup.
- `frontend/src/session.js` and `src/pages/login.js`; the API client now
  refreshes and replays once on a 401, through a single shared in-flight
  refresh. That coalescing is required rather than an optimisation:
  refresh tokens rotate, so four parallel refreshes would have looked like a
  stolen token being replayed and logged the user out.
- Migration `8e5a3c9d21f7`.

### Known limitations

- **The SSRF guard cannot pin a connection to the address it validated** —
  `httpx` offers no supported way, and the workaround breaks certificate
  verification. It re-checks the peer address and discards the response
  instead, so a *blind* request to an internal host remains possible for an
  authenticated caller. Documented in `auth/ssrf.py`.
- Ticket status lookup, the admin API and prompt management are unbuilt —
  which is why the `admin` role currently gates only account creation.
- No CI, no lint or type configuration.

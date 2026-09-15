# Changelog

Notable changes to the AI Customer Assistant. Fuller detail, with the evidence
behind each entry, lives in [`status.md`](status.md) and [`test.md`](test.md).

Entries are grouped by the identifiers those documents use: `P0`/`P1`/`P2` for
problems found by reading the codebase, and `F1`–`F9` for defects found by
running the assembled system against a browser.

## Unreleased — 2026-09-06 to 2026-09-11

The test suite went from **268 passing with 10 failures and 6 errors** to
**969 passing with none**, and the backend grew from ~14.8k to ~21.6k lines
while the tests more than tripled.

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

### Fixed — ingestion

- **Chunk persistence was not idempotent**, which failed 31 of 82 ingestion
  jobs and poisoned documents permanently: re-ingesting a version appended
  its chunks a second time, and the lookup by `(version_id, chunk_index)`
  then raised *"Multiple rows were found when exactly one was required"*
  on every subsequent attempt. `persist_chunks` now replaces rather than
  appends, a `uq_chunk_version_index` constraint makes the broken state
  unrepresentable, and migration `b7d1e93a5c40` cleaned 58 redundant rows
  from the existing data before adding it.
- `_link_entity_to_chunk` no longer raises on a missing chunk — an
  extraction naming an index the document does not have is one bad chunk,
  not a failed document.

### Added — provider API keys

- Admin-only **API Keys** page and `GET/PUT/DELETE /admin/llm-providers`
  (+ `POST .../default`). Groq is the seeded default provider.
- Keys are **AES-GCM encrypted** under a key derived from `AUTH_SECRET`
  (HKDF), and are **never returned over HTTP** — the API exposes the last
  four characters and nothing else.
- Resolution is saved-key first, environment second, so a deployment that
  never opens the page behaves exactly as before. Rotating `AUTH_SECRET`
  invalidates stored provider keys, which must then be re-entered.
- Migration `9a4f7c2b83d1`, with a partial unique index enforcing a single
  default provider in the database.

### Fixed — one thing stored as several entities

- **Identity included the entity type**, so every type the model invented
  minted a new entity. "Agile" was stored three times — `Methodology`,
  `Process`, `Development Process` — holding 39, 14 and 2 facts: 55 facts
  about one concept across three identities the database considered
  unrelated. 62 of 538 entity names were fragmented this way.
- `entity_type` is an *attribute*, not an identity discriminator. Entities now
  resolve by normalized name alone. All 62 fragmented names were reviewed
  first and none was a genuine homonym — every one was the same thing seen
  through a different lens (`Technology` over `Library`, or Instagram as
  Company/Platform/Product).
- `reconcile` merges the existing ones, deciding three things independently:
  the surviving **row** by fact count, the **label** by best casing (so a
  merge cannot rename `PyTorch` to `pytorch`), and the **type** by corpus
  rarity as a specificity proxy — restricted to types that carry a real share
  of the group's facts, or `Development Process` beats `Methodology` on
  rarity alone.
- `PROPER_NOUN_MERGES` is no longer a separate pass; merging by name subsumes
  it. Its reviewed types remain as an override, now keyed by normalized name.
- Two bugs fixed in the repointing: colliding values were matched on exact
  text after uniqueness had moved to `value_norm`, and the survivor was
  relabelled before its duplicates were deleted — which fails when the
  duplicate still holds the chosen `(type, name)`. The second was found only
  by running it against the real database, and the repointing half now has
  tests against real rows.
- Live: 538 → 469 entities, 62 → 0 fragmented names, Agile's facts on one row.
  Values fell 832 → 822 — the ten that were duplicates only because their
  entities were.

### Fixed — one fact stored several times

- **`value` was unique on the exact text**, so `PHP Intern` and `php intern`,
  or `pre-defined` written with an ASCII hyphen and with U+2011, were two
  facts. `value_norm` holds a case-folded, whitespace-collapsed form with
  Unicode punctuation mapped to ASCII, and `(entity, attribute, value_norm)`
  is unique — the duplicate is unrepresentable rather than depending on every
  writer to normalize. Migration `e7b04d2c9a13`, which repoints provenance
  before deleting a duplicate: the FK cascades, so deleting one would drop the
  record that a version asserted the fact and the survivor would then be
  marked superseded.
- **The model restates one fact across two overlapping windows** ("while it is
  not polished" / "while not polished"). These are now folded within a
  document, keeping the longer telling. The similarity threshold (0.92) was
  measured against every duplicate pair in the corpus, not chosen: real
  duplicates and real distinctions overlap below 0.91, so the line sits above
  that band and deliberately misses one true duplicate rather than risk
  merging two different prices.
- **Windows cut mid-word.** The corpus contains the value "smallest yet fun" —
  "smallest yet fun|ctional version of the product" with a boundary through
  the middle of "functional", extracted as a complete fact. No similarity rule
  repairs that (the strings score 0.35 against each other), so windows now end
  on a sentence boundary or whitespace, with a hard cut as the fallback for
  text that has neither.
- `multivalue` is derived from what survives collapsing, not before it —
  otherwise two tellings of one definition read as evidence that the attribute
  takes several values.
- Attributes that genuinely take several values are untouched: `Agile / stage`
  keeps its six stages and `Alpinist Studios / objective` its six objectives,
  both pinned by tests.

### Added — concurrent ingestion lanes and trace ids

- **Every ingestion log line now carries the run that produced it**
  (`[1c66bbf2.1]`). The id names the *run*, not the job: a job can be retried
  four times, so `job_id` alone would label four separate runs identically.
  Carried in a ContextVar rather than a parameter, because the lines that need
  labelling are emitted deep inside modules with no reason to know about jobs
  — and ContextVars propagate through `asyncio.to_thread`, which is where
  extraction runs.
- **The worker runs `PGQUEUE_CONCURRENCY` lanes** (default 2) in one process,
  sharing one copy of the embedding model — a second *process* would load its
  own 400 MB copy, which was the stated blocker. Set to 1 for the previous
  strictly serial behaviour.
- What that buys, stated plainly: extraction is bound by a provider token
  budget, not by this worker, so lanes do not double throughput against a
  daily ceiling. They stop one slow document holding the queue head.
- **A `SentenceTransformer` is not safe to encode from several threads at
  once**, and `chunk_and_embed` runs under `to_thread` with a shared model, so
  lanes would have done exactly that. Encoding is now serialised with a lock;
  it is a small fraction of a document's time.
- **Fixed a race the plan had assumed away.** `FOR UPDATE SKIP LOCKED` stops
  two lanes taking the same row, but the one-job-per-source guard is a
  different question: a lane's RUNNING transition is invisible to the others
  until it commits, so two lanes could each take a *different* job for the
  *same* source. Claims are now serialised for the duration of the claim only.

### Added — fact history and supersession

- **Two contradictory facts could be returned as equally true.** `value` rows
  are unique on (entity, attribute, value), so when a rate changed from $500
  to $800 both survived under the same entity and attribute, with no version
  link and nothing marking which was current — and structured lookup returned
  both. A missing answer is visibly missing; two prices delivered confidently
  is a wrong one.
- History is **kept and marked**, not pruned. `value.superseded_at` is NULL
  for current facts; retrieval filters on it, so the contradiction stops while
  the old rate stays recoverable for a temporal feature later.
- `value_provenance` records which document version asserted which fact. It is
  a link table rather than a column because a `value` row is global — the same
  fact is often stated by several documents — and without that, supersession
  is not decidable: a fact dropped by one document may still be asserted by
  another.
- Currency is **derived, not accumulated**: `resolve_superseded_values`
  recomputes the whole state from provenance each run, so a re-ingest, a
  rollback, or a document ceasing to be current all converge without anyone
  reasoning about order. Facts that come back are un-marked. Values with no
  provenance are never touched, which is why the migration needs no backfill.
- Semantic search over chunks is **deliberately unchanged** — it still sees
  only current versions, so no answer can cite replaced content.
- `Attribute.multivalue` arrived hardcoded `False` from the extractor while 48
  entity+attribute pairs held several values. It is now observed from the
  document: several distinct values for one entity means the attribute takes
  more than one, counted across the whole document and per entity.
- Both value queries gained an `ORDER BY` (newest first). Neither had one, so
  several current values came back in scan order and read as a list of
  equally-weighted facts.
- Migration `d5a91c3f7b28`.

### Fixed — a reaped job waited ten minutes for nothing

- The retry work gave every transient failure a backoff, and a job abandoned
  by a dead worker counts as transient — so an ordinary deploy silently cost
  every in-flight document ten idle minutes before it resumed. A backoff
  answers "the condition that caused this needs time to clear"; a worker that
  died has already cleared, because the process reaping the row is its
  replacement. `retry.IMMEDIATE_KINDS` names the kinds that requeue with no
  delay. The attempt cap still applies to them, which is what actually
  protects against a document that kills every worker that touches it.
- Found by the new worker-loop tests, not in production: the two halves were
  individually correct and only their composition was wrong.

### Added — worker loop tests

- `tests/ingestion/test_worker_loop.py` covers `poll_once` and `run_worker`,
  which had no tests at all despite being where a claimed job, a handler and
  the retry policy meet. Dispatch by `job_type`, one job per tick, the crash
  path that stops a job stranding in RUNNING, all four statuses the retry work
  made possible, startup reaping, the poll-interval sleep, and the
  one-job-per-source guard — which had never been tested despite being what
  makes `FOR UPDATE SKIP LOCKED` safe for more than one worker.

### Added — ingestion retry and dead-letter

- **Every ingestion failure used to be terminal.** `complete_job` wrote
  `FAILED` and that was the end of the document, whatever the reason — worst
  for the most common failure of all, provider throttling, where waiting ten
  minutes is the entire fix. A transient failure now goes back on the queue
  with an exponential backoff (10 minutes, capped at 60), up to four
  attempts.
- **`DEAD_LETTER`**, a terminal state distinct from `FAILED`. `FAILED` means
  "this cannot work as it stands"; `DEAD_LETTER` means "this kept failing for
  a reason that usually passes, and we stopped trying". Both need a human,
  but they need different things from one.
- **`failure_kind`**, a real column beside `error_details`, backfilled from
  the 49 failures already in the database. The retry policy dispatches on it;
  grouping failures no longer means splitting an error sentence on its first
  colon.
- Rate limiting got its own `Err` code, split from `eav_extraction_failed`
  where the exception is still in hand, using the same `is_rate_limited`
  predicate the extraction agent's own backoff uses.
- The policy (`ingestion/queue/retry.py`) is a pure function of failure kind
  and attempt count — no I/O, no clock beyond `now` — so it is unit-testable
  without a database or a provider. Only five failure kinds are transient;
  anything unclassified is terminal, so a new failure kind cannot quietly
  consume a retry budget.
- The stale-job reaper now counts its requeue as an attempt, so a document
  that kills the worker every time dead-letters instead of being requeued
  forever.
- `GET /admin/jobs` serves `failure_kind`, `attempt_count` and
  `next_attempt_at`; the Jobs page gained an **Attempts** column that marks a
  pending retry, because status alone cannot say whether a `QUEUED` row is
  fresh work or a job waiting out its backoff.
- Migration `c4f8b2e17a90`.

### Fixed — ingestion extraction

- **A deterministic model rejection was retried five times.**
  `_is_retryable` matched on the substring `"json"`, and Groq's schema
  rejection reads *"Failed to validate JSON"* — so each one was sent five
  times at `temperature=0` with an identical prompt, for five identical
  failures. Five times the tokens for a guaranteed failure, against the
  200,000-token daily budget that decided whether a document finished at all.
  The clause was written for *flaky* JSON output and was catching a
  *deterministic* rejection.
- **One rejected call failed a whole document.** Batching three windows into
  one call also meant one bad call lost all three and failed the document —
  which is why `sdlc.pdf` and `tech_stck.pdf` sat at zero chunks while the
  other 22 sources indexed. `failed_generation` came back empty: a response
  outgrowing what the model emits for three windows at once, not
  unextractable content. A rejected batch is now split in half and retried,
  recursively, down to single windows.
- Boundaries that keep that from becoming a new bug: only *deterministic*
  rejections split (splitting a rate-limited batch makes two rate-limited
  calls); a document whose every window is rejected still fails, rather than
  succeeding with an empty graph; and a lost window is logged with its chunk
  index.

### Added — ticket status lookup

- Customers can ask what happened to a ticket. `CHECK_TICKET_STATUS` was
  classified correctly and then answered with a fixed
  "status lookups aren't available yet" string, while the `ticket` table held
  real rows — so someone who had just been given a ticket id in a
  confirmation could not ask about it.
- `TicketStore.get_ticket(ticket_id)` is the read half of a store that until
  now only wrote. A new `ticket_status` graph node answers the intent; it
  sits beside the ticket agent rather than inside it, because that agent's
  job is *creating* a ticket and routing a status question there would have
  opened a second one.
- Answers in a single turn when the message carries an id, and `interrupt()`s
  once for the id when it does not — the pause/resume mechanism the
  ticket-creation flow already used, so the serving layer is unchanged.
- **Tickets are found by id, not by email.** An email lookup would be
  friendlier and would let anyone who can name an address read that person's
  tickets. Doing it by email needs the ticket bound to the authenticated
  `AppUser`, which is a data-model change rather than a lookup change.
- A malformed id is a miss rather than an error (the id is typed by a person
  into a chat box), a miss is reported as "not found" rather than as a
  status, and a pasted uppercase id is normalized before lookup so the id
  echoed back matches the confirmation.

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
- The admin *write* surface and prompt management are unbuilt — no source
  deletion, no re-index trigger, no ticket status *change*. A ticket's
  status can be read but only the database can alter it.
- No CI, no lint or type configuration.

# Changelog

Notable changes to the AI Customer Assistant. Fuller detail, with the evidence
behind each entry, lives in [`status.md`](status.md) and [`test.md`](test.md).

Entries are grouped by the identifiers those documents use: `P0`/`P1`/`P2` for
problems found by reading the codebase, and `F1`–`F9` for defects found by
running the assembled system against a browser.

## Unreleased — 2026-09-06

The test suite went from **268 passing with 10 failures and 6 errors** to
**638 passing with none**, and the backend grew from ~14.8k to ~17k lines
while the tests roughly doubled to ~9.1k.

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

### Known limitations

- **No authentication.** Every endpoint is anonymous, CORS allows all
  origins, and the crawler will fetch any URL it is given. This is the one
  blocker before any deployment (`status.md` P0-3).
- Ticket status lookup, the admin API and prompt management are unbuilt.
- No CI, no lint or type configuration, no rate limiting.

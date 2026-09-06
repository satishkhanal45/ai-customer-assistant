# AI Customer Assistant — Project Status Report

**Repository:** `/mnt/hdd/satish/ai-customer-assistant`
**Branch analysed:** `features/graph_visualization` (HEAD `ba45956`, with 7 uncommitted files)
**Report date:** 2026-09-05
**Last updated:** 2026-09-06 — **P0-1**, **P0-2**, all four **P1** items, and **P2-1** fixed; see the changelog
**Method:** full read of `backend/src` (14.8k LOC), `backend/tests` (4.6k LOC), `frontend/src` (2.8k LOC), migrations, Docker/Make tooling and docs; plus a live run of the test suite.

---

## 1. Executive summary

This is a **multi-agent RAG customer-support assistant** for Alpinist Studios, built on FastAPI + LangGraph + Postgres/pgvector, with a document-ingestion pipeline (crawler → MinIO → Tika → chunk/embed → LLM entity extraction → EAV knowledge graph) and a dependency-free vanilla-JS frontend.

**Maturity: a working end-to-end prototype, not yet a deployable product.**

| Dimension | State |
|---|---|
| Architecture & module design | **Strong.** Clean layering, dependency injection everywhere, pure functions separated from I/O, excellent docstrings. |
| Feature completeness (MVP scope) | **~75%.** Chat, RAG, ingestion, crawling, graph browsing and ticket creation all work. Ticket status lookup and the admin surface do not. |
| Test suite | **364 passing / 1 failing / 4 erroring / 2 skipped** (371 collected). Everything still non-green needs only a **Playwright browser** — the suite is otherwise deterministic on a clean checkout. |
| Production readiness | **Low, but the list is shrinking.** Remaining blockers: no authentication, no authorisation, `CORS: *`, debug `print()` of full conversation state, no observability. Secrets are no longer injected by import side effect (P0-2). |
| Scalability | **Much improved.** All four P1 items are fixed: pgvector-native retrieval, LLM calls off the event loop, one shared connection pool, and ingestion moved out of the web process into a worker service. |
| Repo hygiene | **Medium.** The duplicated ontology is gone (P2-1). Still open: dead files, an empty README, debug values committed, 20+ stale branches. |

**P0-1, P0-2, the whole P1 tier, and P2-1 are now fixed** — see the changelog. The two remaining P0 items are both security: **P0-3** (no authentication) and **P0-4** (debug `print()` of conversation state). Everything else is the **P2** quality tier.

### Changelog

**2026-09-06 — P0-2 config loading.** `load_dotenv()` removed from module scope; a single `config.load_env()` is now called by each entry point. This also restored two `skipif` guards that had never worked (they checked for the very variables the import was injecting), so the suite is now deterministic apart from Playwright. Suite 352 → 364 passing, 3 failures → 1.

**2026-09-06 — P2-1 shared ontology.** The two forked ~750-line vocabularies replaced by one shared `ontology` package both pipelines import; three real divergences resolved. The deliberate fuzzy-vs-exact policy difference between reading and writing is preserved and now test-enforced. Ontology code 1500 → 1183 lines. Suite 321 → 352 passing. **Note:** this entry also corrects the original report, which overstated the drift.

**2026-09-05 — P1-2 / P1-3 / P1-4 / P1-5.** LLM calls moved off the event loop (`asyncio.to_thread`); the `?`-splitting in `chat_service` deleted; four database engines consolidated into one pooled engine (`db/engine.py`); ingestion moved out of the API into a `worker` compose service, with a stale-job reaper. Suite 307 → 321 passing.

**2026-09-05 — P1-1 pgvector retrieval.** Ranking pushed into the database with `ORDER BY embedding <=> :q LIMIT :top_k`; embeddings no longer cross the wire. HNSW index added (migration `9f1a2b7c4e08`). Both paths verified identical on live data. 21 tests added to a module that previously had none. Suite 286 → 307 passing.

**2026-09-05 — P0-1 ticket flow repaired.** All four defects fixed, the flow verified end to end against the live database, and the suite moved from 268→286 passing with 7 regressions cleared and 11 new tests added.

---

## 2. Evidence — verified test run

**Baseline, before the ticket-flow fix:**
```
cd backend && env -u PYTHONPATH ./.venv/bin/python -m pytest -q
→ 10 failed, 268 passed, 6 errors in 170.19s
```

**Current, after the P0-1, P0-2, P1 and P2-1 fixes:**
```
cd backend && env -u PYTHONPATH ./.venv/bin/python -m pytest -q
→ 1 failed, 364 passed, 2 skipped, 4 errors in 56.56s
```

Collected: 371 tests. Note the runtime also dropped from ~131s to ~57s: the two opt-in tests that were making real network calls on every run now skip correctly. The 3 remaining failures and 6 errors are unchanged throughout: one is P0-2, the rest need a live Postgres or a Playwright browser.

**Group A — real regressions.** Seven of the eight were the P0-1 ticket flow and are now **fixed**:

| Test | Original error | Now |
|---|---|---|
| `test_agents_wiring.py` ticket flow | `TypeError: No synchronous function provided to "ticket_agent"` | ✅ fixed |
| `test_supervisor.py::test_graph_unknown_exhausted_reaches_ticket_agent` | same | ✅ fixed |
| `test_idempotency.py::test_two_resumes_same_request_id_create_one_row` | same | ✅ fixed |
| `test_idempotency.py::test_same_key_returns_existing_ticket` | `AttributeError: 'coroutine' object has no attribute 'ticket_id'` | ✅ fixed |
| `test_idempotency.py::test_without_key_always_creates_fresh_row` | same | ✅ fixed |
| `test_idempotency.py::test_distinct_keys_create_distinct_rows` | `assert 0 == 2` (`store.rows` empty) | ✅ fixed |
| `test_idempotency.py::test_server_derived_uses_thread_and_sequence` | `assert 't:0' == 't:1'` | ✅ fixed |
| `test_providers.py::test_config_default_anthropic_without_key_falls_back_to_stub` | got `GroqKnowledgeProvider`, expected stub | ❌ **still failing — this is P0-2**, not a ticket-flow defect |

**Group B — environment-dependent.** This list was six entries; **P0-2 reduced it to one cause.**

| Test | Status |
|---|---|
| `tests/ingestion/test_fetcher.py` (4 errors) | needs a Playwright Chromium binary |
| `tests/ingestion/test_crawler.py::test_discover_bfs_traversal_integration` | needs Playwright |
| ~~`tests/db/test_checkpointer.py::test_postgres_checkpointer_builds`~~ | **now skips correctly** — its `skipif` on `POSTGRES_*` finally works |
| ~~`tests/api/test_routes.py` (2 errors)~~ | **now pass** |
| ~~`test_supervisor.py::test_groq_live_classifies_a_greeting`~~ | **now skips correctly** — no longer makes a billed Groq call on every run |

**Why those guards were broken.** Three of them already had `skipif` decorators; they never fired, because `store.py`'s module-scope `load_dotenv()` injected the very variables the guards check for (`POSTGRES_*`, `GROQ_API_KEY`). The tests believed the environment was configured when it was only polluted. Fixing P0-2 fixed the guards.

**What remains.** Only Playwright. Install the browser once and the suite is fully green:

```bash
cd backend && uv run playwright install --with-deps chromium
```

Marking those five `@pytest.mark.integration` (item 5 in §7.1) is still worth doing so a clean checkout is green without a browser at all, but the suite is no longer sensitive to ambient database, credential or rate-limit state.

**Also observed:** running `pytest` without `env -u PYTHONPATH` picks up a conda `pytest` and collapses into 24 collection errors. The correct invocation must go through the project venv — `make test` now does this for you.

---

## 3. Architecture as built

```
 Browser (vanilla JS, hash router)
   │  POST /chat            GET /graph/*          POST /ingest/*
   ▼
 FastAPI  (main.py — also serves the frontend as StaticFiles at "/")
   │
   ├── ChatService ──► Supervisor LangGraph  (Postgres checkpointer)
   │                     ├── classify_and_route   (Groq / Gemini / stub)
   │                     ├── knowledge_agent  ──► Knowledge LangGraph
   │                     │     rewrite → extract → decide_strategy
   │                     │        ├── structured_lookup  (EAV tables)
   │                     │        └── vector_search      (embedding_chunk)
   │                     │     → rank → dedupe → context → prompt → llm
   │                     ├── ticket_agent  (interrupt() × 2 → TicketStore → SMTP)
   │                     └── assemble_response
   │
   ├── /graph/*   read-only EAV graph browser (own engine)
   └── /ingest/*  upload | crawl | discover | confirm  → 202 Accepted
                    └─► register_document_version → MinIO + job row
                                                          │
 worker service (scripts/run_worker.py) ◄─────────────────┘ claims the job
   └─► run_ingestion: fetch → Tika → chunk+embed
        → EAV extraction (Groq) → persist → cutover
```

**Stack:** Python 3.12, FastAPI, LangGraph 1.x, SQLAlchemy 2 (async, psycopg3), Alembic, Postgres 16 + pgvector, MinIO, Apache Tika, Playwright, `sentence-transformers` (BAAI/bge-base-en-v1.5, 768-dim), Groq (`openai/gpt-oss-120b`) as the live LLM.

---

## 4. What is done

### 4.1 Database schema — complete
`backend/src/ai_customer_assistant/db/models.py`, 8 Alembic migrations in a clean linear chain, single head (`0001 → 11160c9078cc → 0003 → 1e4beb1b8d68 → 2a1c9f0e45d7 → 4c2d8a1f9e0b → 7b3e5c1a9d42 → 9f1a2b7c4e08`).

- **EAV core:** `entity`, `attribute`, `value`, `relation` with the right unique constraints for idempotent writes.
- **Document versioning:** `knowledge_source` ⇄ `knowledge_source_version` (the circular FK is correctly handled with `use_alter=True`), `embedding_chunk` (`Vector(768)`, HNSW-indexed on `vector_cosine_ops`), `knowledge_source_entity_map`, `knowledge_injection_job`.
- **`ticket`** table (added in the two most recent commits).
- Soft-delete via `is_active`, version lifecycle `PENDING → PROCESSING → INDEXED → STALE/ARCHIVED/FAILED`.

### 4.2 Ingestion pipeline — complete and well-factored
- **Crawler v2** (`ingestion/crawler/`): Playwright-rendered, sitemap-first discovery with a link-BFS fallback, three configurable wait strategies (`fixed_timeout` / `networkidle` / `selector`), a two-phase `discover` → human review → `crawl_confirmed` flow, HTML→Markdown via trafilatura, and PDF/DOCX download classification.
- **Storage** (`ingestion/storage/`): MinIO client, deterministic key scheme, uploader, repository.
- **Chunk + embed** (`ingestion/chunk_embed/`): 500-token chunks / 75-token overlap, tokenizer-aware recursive splitting, batched normalised BGE embeddings. **97 tests — the best-covered subsystem in the repo.**
- **EAV extraction** (`ingestion/extraction/`): a tool-calling agent over a 762-line canonical ontology, emitting entities / attribute-values / relations per chunk.
- **Persistence** (`ingestion/persistence.py`): entity resolve-or-create with ontology canonicalisation and case-insensitive merge; `ON CONFLICT DO NOTHING` upserts make re-ingestion idempotent.
- **Queue** (`ingestion/queue/`): `knowledge_injection_job` used as a queue table with `FOR UPDATE SKIP LOCKED`, a one-job-per-source guard, chunk-embedding reuse across versions keyed by checksum, and an atomic cutover that flips `current_version_id` and marks the old version `STALE`.
- **Result monad** (`ingestion/result.py`) for short-circuiting stage composition — genuinely elegant.

### 4.3 Knowledge (RAG) Agent — complete
`agents/knowledge/` — 22 modules, all pure functions except two DB modules and the provider layer.

Pipeline: query rewrite (pronoun resolution) → structured-query extraction → strategy decision (`structured` / `vector` / `hybrid`) → retrieval → ranking → deduplication → context assembly → prompt build → grounded answer with `[n]` citations resolved back to `ChunkProvenance`.

Notably correct details: the mandated retrieval join contract (only `current_version_id` + `INDEXED` + `is_active` chunks are visible); session-per-call factories so the hybrid fan-out is concurrency-safe; graceful "found nothing" degradation distinguished from genuine infrastructure failure.

### 4.4 Supervisor Agent — complete
`agents/supervisor/` — LLM classification into `GREETING` / `DOMAIN_REQUEST` / `OUT_OF_SCOPE`, then intent (`KNOWLEDGE_QUERY` / `CREATE_TICKET` / `CHECK_TICKET_STATUS` / `UNKNOWN`), confidence tiering, bounded clarification loop (3 attempts) that escalates into a ticket, and dispatch-table routing with no if/elif chains. A 282-line hand-tuned system prompt carries the Alpinist Studios domain definition. Total parsing — malformed LLM JSON degrades to a safe fallback instead of raising.

### 4.5 Serving layer — complete
- `POST /chat` with per-request `trace_id`; history is **never** accepted from the client — it lives behind the LangGraph checkpointer keyed by `thread_id`.
- Durable `AsyncPostgresSaver` checkpointer with an msgpack allowlist for `ConversationTurn` and `ChunkProvenance`.
- Multi-turn `interrupt()` resume handling for the ticket flow.
- One shared BGE `SentenceTransformer` per process, injected into the Knowledge graph.

### 4.6 Frontend — mostly done
Zero-build vanilla JS (`window.ACA` namespace, classic scripts, hash router), dark/light theming, served same-origin by the backend.

| Page | State |
|---|---|
| Chat | Done — thread list in `localStorage`, retry, citations |
| Graph | Done — 2D/3D force-graph explorer (the current feature branch) |
| Ingest | Done — upload, crawl, discover→review→confirm, job polling |
| Prompt | Partial — prompts are viewable/editable but **device-local only**; no backend write endpoint |
| Overview | Done |
| Admin | **Stub** — renders "not available"; the `/admin/*` endpoints it calls don't exist |

### 4.7 Tooling
`docker-compose.yml` (postgres/pgvector, MinIO + bucket init, Tika, backend), a multi-stage `Dockerfile`, an entrypoint that waits for Postgres and runs `alembic upgrade head`, a `Makefile` with `up/down/worker/ingest/verify/psql/trunc/chat/graph`, and a `langgraph.json` for LangGraph Studio.

---

## 5. What is NOT done

| Gap | Evidence |
|---|---|
| **Authentication / authorisation** | `auth/jwt.py` is **0 bytes**. Every endpoint is anonymous. |
| **Admin API** | `ingestion/storage/api.py:62-83` — all three dependencies `raise NotImplementedError`; the router is deliberately not registered (`main.py:78-80`). |
| **Ticket status lookup** | Routed away at classification time with a hardcoded "not available yet" string (`routing.py:_CHECK_STATUS_UNAVAILABLE`). No read path against the `ticket` table. |
| **Prompt management API** | Frontend edits never reach the server. |
| **`config.py`** | **0 bytes** — every module reads `os.environ` directly instead. |
| **README / CHANGELOG** | `README.md` is one line; `CHANGELOG.md` is empty. |
| **CI** | No `.github/`, no lint config, no formatter config, no coverage gate. |
| ~~**Worker in Docker**~~ | **Done (P1-5)** — a `worker` service now runs `scripts/run_worker.py`; the API only enqueues. |
| **Observability** | No structured logging, no metrics, no tracing. `trace_id` is generated and threaded but never actually logged anywhere. |
| **Streaming responses** | `/chat` is request/response only; a 30–60s RAG turn shows a spinner. |
| **Rate limiting / abuse control** | None, on an endpoint that spends money per call. |

---

## 6. Current problems, ranked

### ✅ P0-1 — The ticket flow was broken end to end — **FIXED 2026-09-05**

**Was:** `agents/ticket_agent/store.py`, `agents/supervisor/agents_wiring.py:194-238`

Four distinct defects, introduced by the last two commits, all now resolved:

| # | Defect | Fix |
|---|---|---|
| 1 | **Async/sync split.** `TicketStore.create_ticket` was made `async`, making the `ticket_agent` node a coroutine, so every `graph.invoke()` caller died with `TypeError: No synchronous function provided to "ticket_agent"`. | Kept the store async — persisting a ticket is genuine `AsyncSession` I/O — and made the whole path async-consistent. The three call sites that still used `invoke()` now use `ainvoke()`, matching production, where the graph is already async because of the Knowledge node. |
| 2 | **The injected session factory was ignored.** `create_ticket` reached for the module-level `db.async_session.session_factory` and used its own `self._session_factory` only as an on/off flag, so an injected factory was silently discarded. | Persistence moved into `TicketStore._persist`, which uses `self._session_factory`. Locked in by `test_store.py::test_uses_the_injected_session_factory`. |
| 3 | **Broken email subject.** `subject = "Your ticket has been created (ID: {ticket_id})"` was missing its `f` prefix, so every customer received literal braces. | Rendering split into a pure `_render_email(ticket) -> (subject, body)` so the wording is testable without SMTP; `test_subject_contains_the_real_ticket_id` asserts the real id is present and no `{` survives. |
| 4 | **The clarifying answer was collected and thrown away.** The node asked *"For what reason do you want to create a ticket?"*, bound the answer to `clarifying_reason`, then called `ticket_ops.call(query)` with the raw message — so the reason never reached `PendingTicket`, the `ticket` row, or the confirmation, despite the docstring claiming otherwise. | `reason` added to `PendingTicket` and `Ticket`, threaded through `call(query, reason)` → `create_ticket` → the `ticket` table (new migration `7b3e5c1a9d42`) → the confirmation email → the reply the customer sees. |

**Also fixed alongside, in the same code path:**

- **SMTP no longer blocks the event loop.** The send ran inline in an `async def`, freezing every other request for up to the 10s socket timeout. It now runs via `asyncio.to_thread`, the same pattern the ingestion pipeline already uses.
- **A bare `TicketStore()` is now inert.** Both the database factory and the email notifier are injected, wired only at the composition root (`build_chat_service`). Previously, merely constructing a store in a test would attempt a real SMTP connection using the credentials `load_dotenv()` had pulled from `backend/.env`.
- **Email is no longer resent on a duplicate.** An idempotent replay returns the cached ticket before reaching persistence or notification.
- **Broad `except Exception: pass` replaced** with a logged warning, so a failed send is diagnosable instead of silent.

**Files changed:**

| File | Change |
|---|---|
| `agents/ticket_agent/types.py` | `reason` field on `PendingTicket` and `Ticket` |
| `agents/ticket_agent/ticket_agent.py` | `call(query, reason=None)`; reason carried through `create_ticket` / `open_ticket` |
| `agents/ticket_agent/store.py` | rewritten: injected collaborators, `_persist`, `_notify`, pure `_render_email`, public `send_ticket_email` |
| `agents/supervisor/agents_wiring.py` | three-step flow documented and wired; reason threaded; `_resume_text` / `_confirmation` helpers |
| `services/chat_service.py` | wires the real notifier at the composition root |
| `db/models.py` | `Ticket.reason` column |
| `alembic/versions/7b3e5c1a9d42_add_ticket_reason.py` | new additive nullable migration |
| `tests/agents/ticket_agent/test_store.py` | **new** — 7 tests for persistence and email rendering |
| `tests/agents/supervisor_agent_test/test_idempotency.py` | async; 4 new tests (reason survival, notifier wiring, no resend on duplicate) |
| `tests/agents/supervisor_agent_test/test_agents_wiring.py` | three-step flow; `FakeTicketOps` updated |
| `tests/agents/supervisor_agent_test/test_supervisor.py` | `ainvoke`; first interrupt is now `clarifying_question` |

**Verification:**
- Suite: 268 → **286 passing**, 10 → **3 failing** (the 7 ticket regressions cleared, 11 tests added).
- Migration chain validated offline: single head `7b3e5c1a9d42`.
- Migration applied to the live database; `\d ticket` confirms the `reason` column.
- End-to-end run against live Postgres through the compiled graph: clarifying question → email prompt carrying the reason → ticket booked → row persisted with `reason = 'duplicate billing charge'` → identical resume returned the same ticket with `len(store.rows) == 1` and no second email.

**Note on behaviour change:** the ticket flow is now genuinely **three-step** (reason → email → create). The older two-step tests asserted `email-collection` as the *first* interrupt; that expectation was stale relative to the Phase 5 clarifying question and has been updated rather than the feature reverted.

---

### ✅ P0-2 — `load_dotenv()` ran at module import — **FIXED 2026-09-06**

**Was:** `agents/ticket_agent/store.py` (top of module)

```python
from dotenv import load_dotenv
load_dotenv()          # executed on import
```

`load_dotenv()` returns nothing useful — it **mutates `os.environ` for the whole process**. So importing a ticket-persistence module silently injected `GROQ_API_KEY`, `SMTP_PASSWORD` and every other secret in `backend/.env` into global state, as a side effect nobody asked for. Three consequences, all reproduced:

1. **Behaviour depended on import order.** The Knowledge provider resolver picks a backend by asking which API keys exist, so its answer changed depending on whether an unrelated module had been imported first. `test_providers.py` **passed alone and failed alongside the ticket tests** — same code, same machine.
2. **Behaviour depended on the working directory.** Bare `load_dotenv()` searches upward from the CWD, so config changed based on where the process was launched.
3. **Secrets entered `os.environ` unintentionally**, putting them in scope for anything that dumps the environment on error.

**What changed.** The previously-empty `config.py` now holds one `load_env()`: path-anchored to `backend/.env` (never the CWD), idempotent, `override=False` so a real environment variable always beats the file, with the manual `KEY=value` fallback `run_worker.py` used to carry inlined for environments without python-dotenv. Loading is now an *entry point's* job:

| Entry point | Before | After |
|---|---|---|
| `main.py` (FastAPI) | **nothing** — relied on `store.py`'s side effect | `load_env()` before project imports |
| `scripts/run_worker.py` | own 20-line loader | `load_env()` |
| `scripts/crawl_and_ingest.py` | own one-liner | `load_env()` |
| `scripts/test_graph_queries.py` | own one-liner | `load_env()` |
| `agents/supervisor/cli.py` | module-scope, CWD-based | `load_env()` inside `main()` |
| `agents/ticket_agent/store.py` | **module-scope `load_dotenv()`** | removed — reads `os.environ` only |

Five ad-hoc implementations became one. No `load_dotenv()` call remains outside `config.py`.

**The result was larger than one test.** Two "opt-in" tests carried `skipif` guards that had never actually worked, because `load_dotenv()` injected the very variables they check for:

| Test | Before | After |
|---|---|---|
| `test_providers.py::...falls_back_to_stub` | FAILED | **passes** |
| `test_checkpointer.py::test_postgres_checkpointer_builds` | FAILED (psycopg) | **skips correctly** |
| `test_supervisor.py::test_groq_live_classifies_a_greeting` | FAILED under load | **skips correctly** — no longer makes a billed network call on a normal run |
| `test_routes.py` (2 tests) | 2 ERRORS (psycopg) | **pass** |

Suite: **352 → 364 passing**, 3 failed → 1, 6 errors → 4. **Every remaining non-green test now needs only a Playwright browser** — the suite is otherwise deterministic on a clean checkout, which was item 5 of §7.1.

**One thing this unmasked.** `test_routes.py::test_chat_roundtrip_persists_thread` then failed on a stale assertion: it still expected the *two*-step ticket flow, which P0-1 replaced with three steps (reason → email → created). The psycopg connection error had been hiding it before it could run. It is a P0-1 leftover, not a P0-2 regression, and has been updated to assert the real flow — including that the customer's stated reason reaches the confirmation.

**Verified at runtime**, since removing an implicit config load is exactly the change that breaks silently:
- Host app boots, `/health` OK, `/graph/search` returns live rows (POSTGRES_* reached it), a real chat turn returns a grounded answer (GROQ_API_KEY reached it).
- `scripts/run_worker.py` starts and claims a job with `POSTGRES_USER`/`PASSWORD`/`DB` supplied **only** by `load_env()` — run with those explicitly unset in the shell.
- Docker rebuilt and healthy; confirmed `/app/.env` **does not exist** in the image (`.dockerignore` excludes it) and `POSTGRES_HOST=postgres` comes from compose, so the container path never depended on the Python-side load.
- A subprocess probe asserts importing `store`, `cli`, `providers` and `chat_service` injects **no** key from `.env`.

**Files changed:** `config.py` (was 0 bytes), `agents/ticket_agent/store.py`, `agents/supervisor/cli.py`, `main.py`, `scripts/run_worker.py`, `scripts/crawl_and_ingest.py`, `scripts/test_graph_queries.py`, `tests/api/test_routes.py`, `tests/test_config_env_loading.py` (new, 10 tests including a guard that fails if a module-scope `load_dotenv()` is reintroduced anywhere).

**Still open:** `config.py` holds only env loading. Modules continue to read `os.environ` directly; turning that into a typed `pydantic-settings` object — the pattern `agents/knowledge/config.py` already uses well — remains a separate improvement (§7.3).

---

### 🔴 P0-3 — No authentication anywhere

**Location:** `main.py:47-52`, `auth/jwt.py` (empty)

```python
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

Anyone who can reach the port can: run unlimited LLM-billed chat turns, upload arbitrary documents into the knowledge base, trigger crawls of arbitrary URLs (a **server-side request forgery** vector — `/ingest/crawl` fetches any URL the caller names, including `http://169.254.169.254/` and internal hosts), read the entire knowledge graph, and create tickets that send email.

`_uploaded_by()` hardcodes the service-account UUID for every ingestion, so there is no attribution either.

**Fix (in order):**
1. Implement `auth/jwt.py` (issue + verify), add a `get_current_user` dependency.
2. Split routers: public = `/chat`, `/health`; authenticated = `/ingest/*`, `/graph/*`; admin-only = the future `/admin/*`.
3. Replace `allow_origins=["*"]` with an env-driven allowlist.
4. Add an SSRF guard on `/ingest/crawl`: block private/loopback/link-local IP ranges after DNS resolution, and enforce a domain allowlist.
5. Add per-IP rate limiting on `/chat` and `/ingest/*`.
6. Set `_uploaded_by()` from the authenticated user.

---

### 🔴 P0-4 — Full conversation state is printed to stdout

**Location:** `agents/supervisor/graph.py:56-79` (`_log_node`)

Every Supervisor node entry and exit does `print(json.dumps(state, indent=2))`. That includes the customer's raw message, the full conversation history, and — on the ticket path — **their email address**. It is unconditional: no log level, no environment guard, and it runs in the Docker image.

Beyond the privacy problem, `print()` is synchronous, unbuffered under `PYTHONUNBUFFERED=1`, and adds real latency per node.

**Fix:** Replace with `logger.debug(...)`, gate on `logging.DEBUG`, and redact `user_message` / `conversation_history` / `email` behind an explicit `LOG_PII=true` flag that defaults to off.

---

### ✅ P1-1 — Vector search did not use pgvector — **FIXED 2026-09-05**

**Was:** `agents/knowledge/vector_search.py`

`_candidate_chunks_statement()` selected **every** live chunk row — including its full 768-float embedding — and `_rank_by_similarity` computed cosine similarity in pure Python. Cost was `O(n)` network transfer plus `O(n·768)` Python float math **per query**, on a schema where `pgvector` was installed and the column was already a real `Vector(768)`.

**What changed.** Ranking is now dialect-aware, decided from the session's own bind so no config setting can drift out of sync with reality:

| Backend | Path |
|---|---|
| PostgreSQL (production) | `ORDER BY embedding <=> :query_vector LIMIT :top_k` pushed into pgvector; the embedding column is **not** selected at all |
| Anything else (SQLite, tests) | the original pure `_rank_by_similarity`, unchanged |

`<=>` is cosine *distance*, so the similarity threshold becomes `distance <= 1 - threshold` and the score is reported back as `1 - distance`. The join contract, the `RetrievedChunk` shape and every downstream helper are untouched — callers cannot tell which path ran.

**Measured on the live database** (32 live chunks, identical query):

| | Python path | pgvector path |
|---|---|---|
| Rows fetched | 32 | 8 |
| Floats shipped over the wire | 24,576 (~96 KB) | **0** |
| Mean retrieval, 20 runs | 210.4 ms | 175.4 ms |

The gap widens linearly with corpus size — the Python path is `O(n)` transfer plus `O(n·768)` arithmetic, while the SQL path returns `top_k` rows regardless of `n`.

**Equivalence verified, not assumed.** Both paths were run against the same live Postgres data: identical chunk ordering, identical similarity scores to 4 decimal places, provenance intact.

**Honest caveat on the HNSW index.** Migration `9f1a2b7c4e08` adds `USING hnsw (embedding vector_cosine_ops)`, built `CONCURRENTLY`. `EXPLAIN` confirms Postgres uses it for a bare single-table `ORDER BY ... LIMIT` — but **not** for this query, because the live-version join contract makes the planner join first and sort after. That is pgvector's well-known pre-filtering limitation, not a mistake in the index. Making it fire under the join needs an ANN pre-filter CTE (`ORDER BY ... LIMIT k * overfetch` over `embedding_chunk` alone, join and re-filter afterwards), which trades exact recall for speed. That trade is deliberately **not** taken here: at this corpus size the sort is cheap, and silently dropping live chunks would be worse. The index is in place for when the corpus makes it worthwhile.

**Files changed:** `agents/knowledge/vector_search.py`, `alembic/versions/9f1a2b7c4e08_hnsw_index_embedding_chunk.py` (new), `tests/agents/knowledge/test_vector_search.py` (new — 21 tests for a module that previously had **none**, covering the pure scorers, the emitted SQL shape, dialect detection, and a SQLite end-to-end run asserting the join contract excludes non-indexed, inactive and superseded content).

**Still open on retrieval quality:** P2-2 below (the missing BGE query instruction and the uncalibrated 0.70 threshold) is unaffected by this change.

---

### ✅ P1-2 — Blocking LLM calls ran on the event loop — **FIXED 2026-09-05**

**Was:** `agents/knowledge/nodes.py`

**Correction to the original diagnosis.** The first version of this report also blamed `GroqSupervisorLLMClient.classify`. That was wrong, and the distinction turned out to be the whole point. Measured directly against LangGraph:

| Node style | Where it runs |
|---|---|
| `def` (sync) | a **thread executor** — blocking inside is harmless |
| `async def` | **directly on the event loop** — blocking inside freezes the process |

The Supervisor's `classify_and_route` is a sync `def`, so LangGraph was already offloading it. The real defect was narrower: the three Knowledge LLM nodes (`rewrite`, `extract`, `llm`) are `async def` and called blocking provider code inline. Three sequential LLM calls per Knowledge turn therefore froze every other request in the process — health checks, other users, concurrent ingestion — for the full duration, and for minutes whenever Groq rate-limited.

**What changed.** Those three calls are wrapped in `asyncio.to_thread`, matching what `ingestion/pipeline.py` already did. The pure stage functions stay synchronous and untouched — the concurrency concern belongs at the graph boundary, not inside `rewriting.py` or `llm.py`, which remain testable without an event loop.

Also fixed: `_MAX_COOLDOWN_WAIT = 300.0` in `providers.py` was **dead** — defined and never referenced, so Groq's "try again in 6m33s" hint was honoured verbatim and unbounded, far exceeding the 120s timeout meant to bound the agent. The backoff is now clamped to it.

**Verified in the running app:** during a real 33-second chat turn the server answered **97** health checks, only 1 of them slower than 1s. The new tests assert the property rather than the implementation — that the completion runs on a non-loop thread, and that a deliberately slow completion does not starve other coroutines — and were confirmed to **fail** when the old inline call is restored.

**Files changed:** `agents/knowledge/nodes.py`, `agents/knowledge/providers.py`, `tests/agents/knowledge/test_event_loop_offload.py` (new, 6 tests).

---

### ✅ P1-3 — Multi-question splitting on `?` corrupted messages — **FIXED 2026-09-05**

**Was:** `services/chat_service.py`

```python
parts = [p.strip() for p in user_message.split("?") if p.strip()]
if len(parts) > 1:
    # run the whole graph once per part
```

Any message containing a `?` anywhere was shredded: `"Can you help? Thanks!"` became two full graph runs and two billed classifications, a URL with a query string was cut in half, `"Really?!"` was split. The interrupt interaction was fragile too — once `had_interrupt` was set it stayed set for every remaining part, silently dropping their history.

**What changed.** The splitting block is deleted; the message always goes to the graph whole. Compound questions are the LLM's job — that is what the prompts are for — not `str.split`. This removes ~55 lines and, on any multi-`?` message, cuts latency and token spend by the number of fragments.

**Files changed:** `services/chat_service.py`.

---

### ✅ P1-4 — Three separate database engines in one process — **FIXED 2026-09-05**

**Was:** engines in `db/session.py` (×2, `lru_cache`d), `db/async_session.py` (module-level), and `api/graph.py` (module-level, built at **import** time) — plus the checkpointer's own pool. None set `pool_size`, `max_overflow` or `pool_pre_ping`, so the real connection ceiling was whatever they happened to add up to, and exhaustion surfaced as an unexplained hang.

Both `api/graph.py` and `db/async_session.py` carried comments asking for this consolidation.

**What changed.** New `db/engine.py` owns the single async engine and session factory for the process, with explicit, env-tunable pool settings (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE`) and `pool_pre_ping`. Everything else delegates:

| Module | Now |
|---|---|
| `db/async_session.py` | thin shim re-exporting from `db.engine`; `session_factory` is lazy, so importing it no longer opens a pool as a side effect |
| `db/session.py` | async helpers are delegating aliases; keeps only the **sync** engine, which is a different driver stack used by Alembic |
| `api/graph.py` | uses the shared `get_session`; its import-time engine is gone |
| `main.py` | disposes the pool on shutdown so a reload does not leak connections |

The LangGraph checkpointer keeps its own psycopg pool — raw psycopg, not SQLAlchemy, so it genuinely cannot share this one.

**Verified:** `grep` confirms one `create_async_engine` in the codebase; the app boots and `/graph/search` returns live rows through the shared engine.

**Files changed:** `db/engine.py` (new), `db/async_session.py`, `db/session.py`, `api/graph.py`, `api/ingest.py`, `main.py`.

---

### ✅ P1-5 — Fire-and-forget ingestion inside the API process — **FIXED 2026-09-05**

**Was:** `api/ingest.py` — `asyncio.create_task(_run_job(job.job_id))`

Five problems in one line: the task reference was dropped so it could be garbage-collected mid-run; concurrency was unbounded (50 uploads meant 50 concurrent pipelines); the 400 MB embedding model, Tika and Groq extraction all ran inside the web worker; a restart orphaned in-flight jobs as permanently `RUNNING` rows, which `claim_next_job`'s one-job-per-source guard then treated as a permanent block on that source; and it duplicated `scripts/run_worker.py`, so the same job had two possible executors.

**What changed.**

1. **The API enqueues only.** `_run_job` is gone; `/ingest/upload`, `/ingest/crawl` and `/ingest/crawl/{id}/confirm` return **202 Accepted** with a `job_id`. The response body shape is unchanged, and the frontend already polled `GET /ingest/jobs/{job_id}` and handled a `pending` result — so no UI change was needed.
2. **A `worker` service in `docker-compose.yml`** runs `scripts/run_worker.py`, waits on postgres/minio/tika, and starts after `backend` so migrations have run. It overrides the entrypoint, since the worker neither migrates nor serves HTTP. Backend and worker share one named image (`ai-customer-assistant:local`) rather than building the same 3 GB context twice — which also makes it impossible for the API and the worker to drift onto different versions of the pipeline.
3. **A stale-job reaper.** `repository.reset_stale_running_jobs` returns jobs stuck `RUNNING` past a threshold back to `QUEUED`; `run_worker` calls it once at startup. Without it, one worker crash blocked a source's ingestion forever with no error anywhere.

**Verified in the live stack.** On first start the worker's reaper found and requeued **7 genuinely abandoned jobs** in the development database — rows left `RUNNING` by earlier crashes that had been permanently blocking ingestion for their sources, silently and with no error recorded anywhere. The worker then claimed and processed them.

**Deployment note:** ingestion now requires the worker to be running. `make up` starts it; if you run the API by hand, run `make worker` alongside it or jobs will sit `QUEUED`.

**Files changed:** `api/ingest.py`, `ingestion/queue/repository.py`, `ingestion/queue/worker.py`, `docker-compose.yml`, `tests/api/test_ingest.py`, `tests/ingestion/test_stale_job_reaper.py` (new, 7 tests).

---

### ✅ P2-1 — The two ontologies had drifted — **FIXED 2026-09-06**

**Correction to the original report.** The first version of this entry claimed a sweeping divergence, citing `corporation`/`firm`/`startup`/`sector` as present in ingestion and missing from retrieval. **That was wrong** — I had grepped only part of the synonym block; retrieval had them all along. The "463 differing lines" figure was a raw text diff, almost entirely docstrings, comments and ordering.

A programmatic comparison of the actual data structures found exactly **three** divergences, not a table's worth. The *class* of bug was real and the impact was real; the *extent* was overstated.

**The three real divergences:**

| Divergence | Ingestion wrote | Retrieval resolved | Consequence |
|---|---|---|---|
| `"data engineer"` | `Person` | **raised `UnknownEntityTypeError`** | a question phrased that way matched nothing |
| `"mobile developer"` | `Person` | `Mobile App` at **0.88 confidence** | a *confident wrong answer* — worse than a failure |
| `Person.salary` | allowed | **raised `UnknownAttributeError`** | a fact that could be stored but never asked for |

None of these raised anywhere in production. They silently lost facts.

**What changed.** A new project-level `ontology` package, belonging to neither `agents` nor `ingestion`, so both import it without depending on each other — preserving the isolation the fork was created for while removing the duplication that made drift possible:

| Module | Role |
|---|---|
| `ontology/vocabulary.py` | the tables — entity types, synonyms, per-type attributes, value types, relations. One copy. |
| `ontology/matching.py` | the pure resolution engine, returning a neutral `Match` and raising nothing |
| `agents/knowledge/ontology.py` | thin adapter: 738 → **154** lines |
| `ingestion/extraction/ontology.py` | thin adapter: 762 → **214** lines |

Total ontology code: **1500 → 1183 lines**, with zero duplicated tables (`MappingProxyType` count: 11 in the shared module, 0 in both adapters).

**A policy difference I nearly destroyed.** The first version of this refactor pushed fuzzy matching into the shared engine for both callers — and broke `test_extraction_tools.py`. Investigating the failure surfaced a deliberate, well-reasoned asymmetry documented in the old ingestion code:

| | Knowledge (reads) | Ingestion (writes) |
|---|---|---|
| entity type / attribute | exact + **fuzzy** | **exact + synonym only** |
| relation type | exact + fuzzy + slug fallback | same |

Ingestion refuses to guess because it *writes to the database*, where a wrong canonical type merges two different real-world entities permanently. `difflib` is false-positive prone on short strings — the codebase's own example is `"customer"` → `"Cluster"`. Retrieval only reads, so a wrong guess costs nothing but an unhelpful answer.

That asymmetry is now an explicit `fuzzy: bool` flag on the shared matcher, set by each adapter, with a test that fails if anyone "harmonises" it away. Both packages also keep their own exception types and the ingestion `safe_*` non-raising variants.

**Verified:**
- Both modules now resolve to the *same table objects* — asserted by identity, not equality, so re-forking the tables fails the build even if the copies start out identical.
- All three divergences resolve identically on both sides.
- Exhaustive sweeps: every entity type and every per-type attribute that ingestion can write is resolvable by retrieval; every synonym resolves to the same term on both sides.
- Suite **321 → 352 passing** (31 new tests), same 3 pre-existing failures.
- Live end-to-end chat turn against the running app returns a grounded answer.

**Files changed:** `ontology/__init__.py`, `ontology/vocabulary.py`, `ontology/matching.py` (all new), `agents/knowledge/ontology.py`, `ingestion/extraction/ontology.py`, `tests/ontology/test_shared_vocabulary.py` (new, 31 tests).

---

### 🟡 P2-2 — Retrieval quality is untuned

Two concrete items in `agents/knowledge/` and `services/embeddings.py`:

1. **Missing BGE query instruction.** `SharedEmbeddings.embed_query` (`services/embeddings.py:50-56`) encodes the query with no prefix. BAAI's `bge-*-en-v1.5` models are trained for asymmetric retrieval and document the query-side instruction `"Represent this sentence for searching relevant passages: "`. Index-time passages correctly get no prefix; the query side should get one. Worth A/B testing before adopting — but as written, query and passage embeddings are being produced under a different convention than the model was trained for.

2. **Hard absolute similarity cutoff.** `DEFAULT_SIMILARITY_THRESHOLD = 0.70` (`constants.py`) is applied as an absolute floor, and falling below it raises `EmptyRetrievalError`. Normalised BGE cosine scores for *unrelated* text commonly land around 0.6–0.7, so this threshold is simultaneously too permissive (noise passes) and too strict (valid answers are dropped). No evaluation set exists to calibrate it against.

**Fix:** Add the query instruction behind a config flag; build a ~50-question golden set from the ingested Alpinist Studios corpus; measure recall@k and answer groundedness; then set the threshold from data. Consider relative scoring (keep results within X% of the top score) instead of an absolute floor.

---

### 🟡 P2-3 — Failed ingestion can commit partial data

**Location:** `ingestion/pipeline.py:172-259`

`_stage_persist` writes chunks and EAV rows through `session.add`/`flush` without committing. If a later stage fails, the `Err` branch calls `job_repo.mark_version_status(session, ..., FAILED)` — which calls `session.commit()`, **committing the pending partial writes** along with the status change.

Impact is bounded: the retrieval join contract filters on `current_version_id` + `INDEXED`, so these orphan chunks are invisible to search. But they consume storage, distort `chunks_created_count` reporting, and will confuse anyone reading the tables directly.

**Fix:** Wrap the pipeline in an explicit transaction and `await session.rollback()` before writing the `FAILED` status — or write the status through a separate short-lived session.

---

### 🟡 P2-4 — Frontend/backend timeout mismatch

`frontend/src/pages/chat.js:298` sends `/chat` with a 60 000 ms timeout. The backend's Knowledge node timeout is `knowledge_timeout_s = 120` (`agents/supervisor/graph.py`), and the Groq provider's own retry ceiling is 300 s.

A turn taking 60–120 s is aborted in the browser while the server completes it and writes the checkpoint. The user sees "Request timed out", the answer is lost, and the next message resumes from a state they never saw.

**Fix:** Make the backend budget strictly smaller than the client budget (e.g. node 45 s, client 60 s), and/or stream the response so the connection stays alive.

---

### 🟡 P2-5 — In-memory state defeats horizontal scaling

| State | Location | Consequence |
|---|---|---|
| `_discovery_cache` | `api/ingest.py:59` | A `POST /crawl/discover` on instance A cannot be confirmed on instance B → 404 |
| `TicketStore._by_key` | `store.py:791` | Idempotency guarantee holds per-process only; two instances can double-create |
| `TicketStore.rows` | `store.py:792` | Unbounded in-memory growth for the process lifetime |

**Fix:** Move discovery caching to Redis or a `crawl_discovery` table with a TTL column; move ticket idempotency to a unique index on `ticket.idempotency_key` and let the database enforce it.

---

### 🟡 P2-6 — Repository hygiene

| Item | Detail |
|---|---|
| `echo` (repo root) | **Tracked in git.** Contains a stray AI-assistant summary: *"Task complete. The ticket agent now has: 1. Database persistence…"*. Should be deleted. |
| `backend/store_new.py` | 190-line near-duplicate of `agents/ticket_agent/store.py`. Dead. Delete. |
| `api/chat.py` | An entire unused router (`ChatRequest`/`ChatResponse`/`answer()`), never registered — `main.py:77` explains it collides with the real `/chat`. Delete. |
| `config.py`, `auth/jwt.py` | 0 bytes. Either implement or remove. |
| `output/docs.md` | Tracked 8-line crawl artefact from `example.com`. Should be gitignored. |
| `frontend/app/.vite/deps_temp_*` | Stray Vite temp directory in a project with no build step. |
| **Debug values committed to `CrawlConfig`** | `request_timeout: 520.0` (was 15.0) and `max_pages: 5` (was 50) — uncommitted working-tree edits that look like debugging leftovers, not intentional configuration. |
| `README.md` | One line: `# AI Customer Assistant`. No setup instructions exist anywhere except scattered docstrings. |
| Branches | 13 local + 24 remote branches, many long-merged. |
| `.pytest_cache` | Present at repo root (gitignored, but noise). |

---

## 7. Recommended improvements

### 7.1 Correctness & safety (do first)
1. ~~Fix the ticket flow (P0-1).~~ **Done 2026-09-05** — 7 regressions cleared, 11 tests added.
2. ~~Remove import-time `load_dotenv()` (P0-2).~~ **Done 2026-09-06**
3. Implement JWT auth + CORS allowlist + SSRF guard on the crawler (P0-3).
4. Replace `print()` node logging with redacted structured logging (P0-4).
5. Mark DB/Playwright tests with `@pytest.mark.integration` and add a `-m "not integration"` default so the unit suite is green on a clean checkout.

### 7.2 Performance & scale
6. ~~pgvector `<=>` + HNSW index (P1-1).~~ **Done 2026-09-05** — see the P1-1 entry.
7. ~~Non-blocking LLM calls (P1-2).~~ **Done 2026-09-05**
8. ~~Remove the `?` splitter (P1-3).~~ **Done 2026-09-05**
9. ~~Consolidate to one database engine with explicit pool sizing (P1-4).~~ **Done 2026-09-05**
10. ~~Move ingestion out of the API into the worker; add it to `docker-compose` (P1-5).~~ **Done 2026-09-05**
11. Add a Redis cache for repeated identical queries (embedding + answer) — support traffic is heavily repetitive.

### 7.3 Quality
12. ~~Unify the ontologies with a drift test (P2-1).~~ **Done 2026-09-06**
13. Build a golden Q&A evaluation set; calibrate `similarity_threshold` and `top_k` against it (P2-2).
14. Add a re-ranking stage (cross-encoder) — currently ranking is a weighted score with no second pass.
15. Wrap ingestion in a proper transaction boundary (P2-3).
16. Add `ruff` + `mypy` and a GitHub Actions workflow: lint → type-check → unit tests → coverage gate.

### 7.4 Features
17. **Ticket status lookup** — the `ticket` table exists and the `CHECK_TICKET_STATUS` intent is already classified; only the read path is missing. Cheapest remaining MVP feature.
18. **Admin API** — implement the three `NotImplementedError` dependencies in `ingestion/storage/api.py`, add `/admin/sources|jobs|stats|tickets`, and register the router. The frontend page is already written and waiting.
19. **Prompt management endpoint** so the Prompt page's edits persist server-side and are versioned.
20. **Streaming `/chat`** via SSE — eliminates the timeout mismatch and transforms perceived latency.
21. **Ticket lifecycle** — `updated_at`, `resolved_at`, assignee, a `thread_id` FK linking a ticket back to its conversation, and inbound email replies.

### 7.5 Operations
22. Structured JSON logging that actually emits the `trace_id` already threaded through every node.
23. Prometheus metrics: chat latency by stage, retrieval hit rate, LLM token spend, job queue depth.
24. `/health` should check Postgres, MinIO and Tika — it currently returns `{"status": "ok"}` unconditionally.
25. A stale-job reaper (see P1-5).
26. Move secrets to a secret manager; `backend/.env` currently holds a live `GROQ_API_KEY` and SMTP password in plaintext on disk (correctly gitignored, but not protected).
27. Write the README: prerequisites, `make up`, migrations, `playwright install chromium`, the `env -u PYTHONPATH` test invocation, and the architecture diagram.

---

## 8. Quick wins (under an hour each, high value)

- [x] ~~Add the missing `f` to the email subject~~ — **done** (P0-1)
- [ ] `git rm echo backend/store_new.py backend/src/ai_customer_assistant/api/chat.py`
- [ ] Delete the `?` splitting block — `chat_service.py:138-194`
- [ ] Guard `_log_node` behind `if os.environ.get("DEBUG_GRAPH")` — `graph.py:56`
- [ ] `allow_origins` from an env var — `main.py:49`
- [ ] Revert `CrawlConfig.request_timeout` to 15.0 and `max_pages` to 50
- [ ] Add `output/`, `frontend/app/` to `.gitignore`
- [x] ~~Use `self._session_factory` in `TicketStore.create_ticket`~~ — **done** (P0-1)
- [ ] Make `/health` check the database
- [ ] Write a real README

---

## 9. Reference

**Run the tests correctly** (a conda `pytest` on `PATH` otherwise shadows the venv and produces 24 spurious collection errors):
```bash
cd backend
env -u PYTHONPATH ./.venv/bin/python -m pytest -q
```

**Run the stack:**
```bash
make up                      # postgres + minio + tika + backend
make worker                  # ingestion worker (separate terminal)
make ingest URL=... SITE=N   # crawl + ingest a URL
make chat                    # open http://127.0.0.1:8002/#/chat
make verify                  # list knowledge sources
```

**Apply migrations from the host** (the compose Postgres is published on 5433):
```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH PYTHONPATH=src/ai_customer_assistant ./.venv/bin/python -m alembic upgrade head
```

**Codebase size:** backend `src` 14 809 lines · tests 4 628 lines · frontend 2 773 lines.

**Largest modules:** `ingestion/extraction/ontology.py` (762) · `agents/knowledge/ontology.py` (738) · `frontend/src/pages/graph.js` (930) · `frontend/src/pages/ingest.js` (509) · `agents/knowledge/structured_lookup.py` (455) · `api/ingest.py` (432).

---

## 10. Verdict

The engineering *discipline* in this codebase is unusually good: consistent dependency injection, pure functions isolated from I/O, immutable data types, dispatch tables in place of conditional chains, and docstrings that explain *why* rather than *what*. Whoever wrote the Knowledge Agent and the ingestion pipeline knew what they were doing.

What is missing is the last mile. The system was built module-by-module against a plan document, and the seams show where the modules meet: the ticket flow half-migrated to async and broke (now repaired), the two ontologies forked and drifted, four database engines accumulated, and every cross-cutting concern that no single module owns — auth, logging, config, CI, the README — is simply absent.

**With P0 + P1 addressed (roughly 2–3 focused weeks), this becomes a genuinely deployable internal product.**

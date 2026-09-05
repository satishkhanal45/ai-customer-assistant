# AI Customer Assistant — Project Status Report

**Repository:** `/mnt/hdd/satish/ai-customer-assistant`
**Branch analysed:** `features/graph_visualization` (HEAD `ba45956`, with 7 uncommitted files)
**Report date:** 2026-09-05
**Last updated:** 2026-09-05 — **P0-1 (ticket flow)** and **P1-1 (pgvector retrieval)** fixed; see the changelog
**Method:** full read of `backend/src` (14.8k LOC), `backend/tests` (4.6k LOC), `frontend/src` (2.8k LOC), migrations, Docker/Make tooling and docs; plus a live run of the test suite.

---

## 1. Executive summary

This is a **multi-agent RAG customer-support assistant** for Alpinist Studios, built on FastAPI + LangGraph + Postgres/pgvector, with a document-ingestion pipeline (crawler → MinIO → Tika → chunk/embed → LLM entity extraction → EAV knowledge graph) and a dependency-free vanilla-JS frontend.

**Maturity: a working end-to-end prototype, not yet a deployable product.**

| Dimension | State |
|---|---|
| Architecture & module design | **Strong.** Clean layering, dependency injection everywhere, pure functions separated from I/O, excellent docstrings. |
| Feature completeness (MVP scope) | **~75%.** Chat, RAG, ingestion, crawling, graph browsing and ticket creation all work. Ticket status lookup and the admin surface do not. |
| Test suite | **307 passing / 3 failing / 6 erroring** (316 collected). Of what remains, 1 failure is P0-2 and the other 8 need a live Postgres or a Playwright browser. |
| Production readiness | **Low.** No authentication, no authorisation, `CORS: *`, secrets read ad hoc, debug `print()` of full conversation state, no observability. |
| Scalability | **Improving.** Vector search now ranks inside pgvector (P1-1 fixed). Still open: blocking LLM calls on the event loop, and ingestion running inside the API process. |
| Repo hygiene | **Medium.** Dead files, duplicated modules, an empty README, debug values committed, 20+ stale branches. |

The **ticket flow (P0-1)** and **pgvector-native retrieval (P1-1)** are now fixed — see the changelog. The next most important items are **P0-2 (import-time `load_dotenv()`)**, **P0-3 (no authentication)**, **P0-4 (debug `print()` of conversation state)**, and then **P1-2 (blocking LLM calls on the event loop)**.

### Changelog

**2026-09-05 — P1-1 pgvector retrieval.** Ranking pushed into the database with `ORDER BY embedding <=> :q LIMIT :top_k`; embeddings no longer cross the wire. HNSW index added (migration `9f1a2b7c4e08`). Both paths verified identical on live data. 21 tests added to a module that previously had none. Suite 286 → 307 passing.

**2026-09-05 — P0-1 ticket flow repaired.** All four defects fixed, the flow verified end to end against the live database, and the suite moved from 268→286 passing with 7 regressions cleared and 11 new tests added.

---

## 2. Evidence — verified test run

**Baseline, before the ticket-flow fix:**
```
cd backend && env -u PYTHONPATH ./.venv/bin/python -m pytest -q
→ 10 failed, 268 passed, 6 errors in 170.19s
```

**Current, after the ticket-flow and pgvector fixes:**
```
cd backend && env -u PYTHONPATH ./.venv/bin/python -m pytest -q
→ 3 failed, 307 passed, 6 errors in 135.12s
```

Collected: 316 tests — `tests/agents` 134, `tests/chunk_embed` 97, `tests/ingestion` 63, `tests/services` 16, `tests/api` 6, `tests/db` 4.

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

**Group B — environment-dependent, no skip markers (should be marked, not "fixed"):**

| Test | Cause |
|---|---|
| `tests/db/test_checkpointer.py::test_postgres_checkpointer_builds` | needs a live Postgres |
| `tests/api/test_routes.py` (2 errors) | needs a live Postgres |
| `tests/ingestion/test_fetcher.py` (4 errors) | needs a Playwright Chromium binary |
| `tests/ingestion/test_crawler.py::test_discover_bfs_traversal_integration` | needs Playwright |

**Also observed:** running `pytest` without `env -u PYTHONPATH` picks up a conda `pytest` and collapses into 24 collection errors. The correct invocation must go through the project venv.

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
   └── /ingest/*  upload | crawl | discover | confirm
                    └─► register_document_version → MinIO + job row
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
| **Worker in Docker** | `docker-compose.yml` has no worker service; ingestion only runs inside the API process or via a manual `make worker`. |
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

### 🔴 P0-2 — `load_dotenv()` runs at module import

**Location:** `agents/ticket_agent/store.py` (top of module)

```python
import os
from dotenv import load_dotenv
load_dotenv()          # ← executes on import, before any imports below it
```

Importing a ticket-persistence module mutates the process environment. Consequences, both observed:
- `tests/agents/knowledge/test_providers.py` fails because `GROQ_API_KEY` leaks into the test environment from `backend/.env` and the provider resolver returns Groq instead of the expected stub.
- Behaviour depends on the current working directory, so tests pass or fail depending on where `pytest` is launched.

**Status:** left in place deliberately during the P0-1 fix (it is a separate change), and now flagged with a comment at the call site. It is the *only* remaining non-environmental test failure in the suite.

**Fix:** Delete the call. Load `.env` exactly once, explicitly, at the process entry points (`main.py` lifespan, `scripts/run_worker.py`, `scripts/crawl_and_ingest.py`). `scripts/run_worker.py` already does this correctly — copy that pattern.

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

### 🟠 P1-2 — Blocking LLM calls run on the event loop

**Location:** `agents/knowledge/nodes.py` (all nine node factories), `agents/knowledge/providers.py`

Every Knowledge node is declared `async def`, but the work inside is synchronous and blocking:
- `rewrite_query`, `extract_query`, `generate_response` are plain sync functions.
- `GroqKnowledgeProvider._complete` uses the blocking Groq SDK and `time.sleep()` for backoff (up to a **300-second** cooldown, `providers.py:41`).
- `GeminiKnowledgeProvider._generate` uses blocking `requests.post`.
- `GroqSupervisorLLMClient.classify` likewise, plus `time.sleep()` retries.

A single chat turn therefore freezes the **entire** FastAPI process — health checks, ingestion jobs, every other user's request — for the full duration of 3 sequential LLM calls. If Groq rate-limits, the server is unresponsive for up to five minutes.

The ingestion pipeline already gets this right (`pipeline.py:158-164` wraps its LLM call in `asyncio.to_thread`) — the same pattern just needs applying here.

**Fix:** Wrap each provider call in `asyncio.to_thread(...)`, or switch to the async SDK clients (`groq.AsyncGroq`, `httpx.AsyncClient`) and make the completion callables awaitable. Replace `time.sleep` with `await asyncio.sleep`. Add a hard per-call timeout — the 300s cooldown ceiling is longer than the 120s node timeout that is supposed to bound it.

---

### 🟠 P1-3 — Multi-question splitting on `?` corrupts messages

**Location:** `services/chat_service.py:138-194`

```python
parts = [p.strip() for p in user_message.split("?") if p.strip()]
if len(parts) > 1:
    # run the whole graph once per part
```

Any message containing a `?` anywhere is shredded:
- `"Can you help? Thanks!"` → two graph invocations, two LLM classification calls, two billed turns.
- `"What is the URL for https://x.com/a?b=1"` → split mid-URL.
- `"Really?!"` → split.

Each part is also a full graph run, so an N-`?` message costs N× latency and N× tokens. The interrupt interaction is fragile too: once `had_interrupt` is set it stays set for all remaining parts, so history for those parts is silently dropped.

**Fix:** Delete the splitting. Send the message whole and let the LLM handle compound questions — that is what the prompt is for. If genuine multi-question handling is wanted later, do it inside the Knowledge Agent with a proper decomposition step, not with `str.split`.

---

### 🟠 P1-4 — Three separate database engines in one process

| Engine | Location |
|---|---|
| Sync engine | `db/session.py:22` (`lru_cache`) |
| Async engine #1 | `db/session.py:44` (`lru_cache`) |
| Async engine #2 | `db/async_session.py:24` (module-level) |
| Async engine #3 | `api/graph.py:51` (module-level, created **at import time**) |
| Connection pool #4 | `db/checkpointer.py` — a separate `AsyncConnectionPool` for LangGraph |

Both `api/graph.py:10-17` and `db/async_session.py:5-8` contain comments acknowledging the duplication and asking for it to be consolidated. None of the extra engines set `pool_size`, `max_overflow` or `pool_pre_ping`, so pool exhaustion under load is likely and hard to diagnose.

**Fix:** One `db/engine.py` owning a single async engine with explicit pool settings; every module takes the session factory by injection. Delete the module-level engines in `api/graph.py` and `db/async_session.py`.

---

### 🟠 P1-5 — Fire-and-forget ingestion inside the API process

**Location:** `api/ingest.py:181`

```python
asyncio.create_task(_run_job(job.job_id))
```

Problems:
- The task reference is not retained, so it can be garbage-collected mid-run (a documented `asyncio` footgun).
- Unbounded concurrency: 50 uploads spawn 50 concurrent jobs.
- Runs the 400 MB BGE model, Tika calls and Groq extraction inside the web worker.
- On restart, in-flight jobs are lost — the row stays `RUNNING` forever, and the one-job-per-source guard then blocks that source permanently.
- Fully duplicates the `scripts/run_worker.py` execution path, so there are two ways for a job to run and they can race.

**Fix:** Delete `_run_job` from the API. The endpoint should enqueue only and return `202` with the `job_id`; the frontend already polls `GET /ingest/jobs/{job_id}`. Add a `worker` service to `docker-compose.yml` running `scripts/run_worker.py`. Add a startup reaper that resets stale `RUNNING` jobs (`started_at` older than N minutes) back to `QUEUED`.

---

### 🟡 P2-1 — The two ontologies have already drifted

`agents/knowledge/ontology.py` (738 lines) and `ingestion/extraction/ontology.py` (762 lines) are forked copies — `diff` reports 463 differing lines. The ingestion copy's docstring says the fork is intentional ("ingestion never depends on the agent package"), but the vocabularies are no longer in sync:

| Synonym | Ingestion | Knowledge |
|---|---|---|
| `corporation`, `firm`, `startup` → `Company` | ✅ | ❌ |
| `sector`, `non-profit`, `nonprofits` → `Industry` | ✅ | ❌ |
| `market segment` → `Industry` | ✅ | ❌ |

**This silently breaks retrieval.** Ingestion canonicalises `"startup"` to entity type `Company` and writes that row; a user asking *"which startups do you work with?"* goes through the *knowledge* ontology, which cannot canonicalise `"startup"`, so `structured_lookup` misses a fact that is sitting in the database.

**Fix:** Extract the shared vocabulary into one module (`ontology/vocabulary.py`) that both import, keeping any genuinely stage-specific behaviour as thin wrappers. Add a test asserting `ingestion.canonical_types ⊆ knowledge.canonical_types` so drift fails CI.

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
2. Remove import-time `load_dotenv()` (P0-2) — now the only non-environmental failure left.
3. Implement JWT auth + CORS allowlist + SSRF guard on the crawler (P0-3).
4. Replace `print()` node logging with redacted structured logging (P0-4).
5. Mark DB/Playwright tests with `@pytest.mark.integration` and add a `-m "not integration"` default so the unit suite is green on a clean checkout.

### 7.2 Performance & scale
6. ~~pgvector `<=>` + HNSW index (P1-1).~~ **Done 2026-09-05** — see the P1-1 entry.
7. Non-blocking LLM calls (P1-2).
8. Remove the `?` splitter (P1-3) — cuts LLM cost immediately.
9. Consolidate to one database engine with explicit pool sizing (P1-4).
10. Move ingestion out of the API into the worker; add it to `docker-compose` (P1-5).
11. Add a Redis cache for repeated identical queries (embedding + answer) — support traffic is heavily repetitive.

### 7.3 Quality
12. Unify the ontologies with a drift test (P2-1).
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

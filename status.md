# AI Customer Assistant — Project Status Report

**Repository:** `/mnt/hdd/satish/ai-customer-assistant`
**Branch:** `features/test` (last commit `ff24bd4`; the P0-4 / F1–F9 work is uncommitted at the time of writing)
**Report date:** 2026-09-05
**Last updated:** 2026-09-06 — **P0-1**, **P0-2**, the **whole P1 tier** (including the newly-found P1-6), the **whole P2 tier**, and **F1–F7 + F9** from the live UI test; see the changelog
**Method:** full read of `backend/src` (14.8k LOC), `backend/tests` (4.6k LOC), `frontend/src` (2.8k LOC), migrations, Docker/Make tooling and docs; plus a live run of the test suite.

---

## 1. Executive summary

This is a **multi-agent RAG customer-support assistant** for Alpinist Studios, built on FastAPI + LangGraph + Postgres/pgvector, with a document-ingestion pipeline (crawler → MinIO → Tika → chunk/embed → LLM entity extraction → EAV knowledge graph) and a dependency-free vanilla-JS frontend.

**Maturity: a working end-to-end prototype, not yet a deployable product.**

| Dimension | State |
|---|---|
| Architecture & module design | **Strong.** Clean layering, dependency injection everywhere, pure functions separated from I/O, excellent docstrings. |
| Feature completeness (MVP scope) | **~75%.** Chat, RAG, ingestion, crawling, graph browsing and ticket creation all work. Ticket status lookup and the admin surface do not. |
| Test suite | **638 passing / 0 failing / 0 erroring / 2 skipped** (640 collected). **Fully green** — the Playwright browser is now installed, so the last non-deterministic gap is closed. |
| Production readiness | **One blocker left.** No authentication, no authorisation, `CORS: *` (P0-3). Conversation state no longer reaches the logs (P0-4). Secrets are no longer injected by import side effect (P0-2), and the client/server timeout ladder no longer inverts (P2-4). |
| Scalability | **Much improved.** All four P1 items are fixed: pgvector-native retrieval, LLM calls off the event loop, one shared connection pool, and ingestion moved out of the web process into a worker service. Two pieces of per-process state that quietly broke horizontal scaling now live in the database (P2-5). |
| Repo hygiene | **Good.** The duplicated ontology is gone (P2-1); dead files, the committed AI-assistant note, the crawl artefact and the committed debug values are all gone, and the README is real (P2-6). Only the stale branches are left, deliberately untouched. |

**Every P0 except one, and the whole P1 and P2 tiers, are now fixed** — see the changelog. What remains is **P0-3**: no authentication, no authorisation, `CORS: *`, and an unguarded crawler that will fetch any URL it is given. It is now the single thing standing between this and a deployable internal product.

### Changelog

**2026-09-06 — F8, `hybrid_retrieve` deleted.** It took a single `AsyncSession` while running both retrieval arms under `asyncio.gather`, so the one strategy it was named after raised `InvalidRequestError`. That was fixed with a session factory — and then the function was removed instead, because it had **no production callers** and duplicated four behaviours the compiled graph already implemented (strategy dispatch, graceful misses, the P1-6 fallback, the F4 filter), each of which had already needed matching edits in both copies. `hybrid.py` is now 151 lines of pure retrieval policy rather than 337. **Retrieval is unchanged**: verified live afterwards at `strategy=hybrid, facts=4, chunks=1` — hybrid search is a property of the graph's fan-out edge, not of the function named after it. Eleven test call sites moved onto the nodes and the compiled graph. Suite 645 → **638 passing**.

**2026-09-06 — F8 (superseded), `hybrid_retrieve` made usable.** It took a single `AsyncSession` while running both retrieval arms under `asyncio.gather`, so the one strategy it is named after raised `InvalidRequestError` — and since F5 routes almost everything to hybrid, that was close to always. It now takes a session factory and opens one per arm, as `nodes.py` always has. Recorded honestly: this function has **no production callers** and duplicates orchestration the graph implements independently; deleting it was the alternative considered, and keeping it was a deliberate decision. The duplication has already cost double edits twice (P1-6, F4). Suite 642 → **645 passing**.

**2026-09-06 — P0-4, conversation state off the logs.** `_log_node` printed the full Supervisor state on every node entry and exit — the customer's message, the whole history, and their email address inside the ticket confirmation — unconditionally, in the Docker image. Replaced by a `DEBUG`-level tracer that checks whether it is enabled *before* serialising, and redacts free text to a shape summary even when it is, so routing stays debuggable without reproducing what anyone said. `LOG_PII=true` is the deliberate opt-in. Verified with a turn carrying a card number and an email address: zero occurrences in the container log, and per-turn log volume down from hundreds of lines to 18. Suite 613 → **642 passing**.

**2026-09-06 — F9, a structured fact was deleting the answer.** `deduplicate` dropped any chunk containing both a fact's value and its entity label, as "redundant prose". For *"Why did Soani Tech change its name?"* that discarded the entire 2463-character press release — because the graph held `Alpinist Studios formerly_known_as "Soani Tech"` and a rebrand announcement necessarily names both. The documentation section was left literally empty, so the model's refusal was *correct*. Two earlier hypotheses (an over-strict answer prompt; an empty-string fact value) were each killed by measurement before the real cause turned up. The pass is removed, not tightened. Cleaned up alongside: the dead `groundedness_threshold`, the discarded `is_grounded` (the node hardcoded `"GROUNDED"`, so refusals logged as successes — and **a test was pinning that in place**), and a real order-preservation bug in `deduplicate` that my own new test caught. The module had had no tests at all. Suite 604 → **613 passing**.

**2026-09-06 — F6, corpus-aware scope.** The Supervisor decided what was in scope from a hand-written paragraph with no connection to the knowledge base, and confidently refused *"Why did Soani Tech change its name?"* while a document called `soani-tech-is-now-alpinist-studios` sat in the corpus. **The fix originally proposed for this was refuted by measurement**: routing low-confidence out-of-scope classifications through retrieval would have done nothing, because that question scored 0.95 and "how's the weather" scored 0.98 — the classifier is sure, and wrong. The domain definition now carries the titles of the live documents, refreshed on a TTL so ingestion widens scope without a restart, degrading to the static paragraph whenever the database is unreachable. Measured: the refused question flips to `DOMAIN_REQUEST` while "how's the weather" stays `OUT_OF_SCOPE`. Suite 589 → **604 passing**.

**2026-09-06 — F5, deterministic routing.** `decide_strategy` keyed on the *shape* of a non-deterministic extraction, so the same question retrieved differently on different runs — `Policy`/`supports`/0.85 routed to structured-only (no semantic search at all) while `Company`/no-slot/0.62 routed to hybrid. Routing now asks one question, *is there an entity to look up?*, and consults neither shape nor confidence; the second lever turned out to matter too (0.54 vs 0.56 flipped vector/hybrid). `STRATEGY_STRUCTURED` is no longer selectable, though it is kept as the backstop carrying the P1-6 fallback. A new `route` node finally populates `KnowledgeAgentState.retrieval_strategy` — declared and documented since the beginning, never written, because conditional edges cannot write state. Fixed alongside: **application `INFO` logging never appeared at all** (uvicorn leaves the root at WARNING), so the new line — and any other — was being discarded in every deployed process; `logging_config.configure_logging()` now runs at startup. Suite 521 → **589 passing**.

**2026-09-06 — F7, streamed responses.** A median turn takes 35s and the customer saw an animated ellipsis for all of it, with no way to tell a working system from a hung one. A new `POST /chat/stream` returns server-sent events — a `trace_id` up front, then stage transitions, heartbeats through the silence, and the same answer the buffered endpoint gives. `POST /chat` is unchanged and is the fallback. Measured in the browser, the stage line now appears at **0.05s** and updates as the turn progresses. Both paths share `_prepare_turn` / `_finalize_turn`, so the ticket/interrupt flow cannot diverge between them; hanging up cancels the graph, and the F1 turn budget still applies. Suite 506 → **521 passing**.

**2026-09-06 — F4, structured-fact relevance.** `structured_lookup`'s *general* shape returns everything known about an entity, and that dump went into the prompt under "Structured Facts" ahead of the documentation — with `answer.md` rule 4 telling the model to *prefer* it. Measured live on "What are the core values of the company?": 15 facts, all about Agile ceremonies, while the documentation right below contained the answer. **The run that retrieved more was the run that failed** (facts=15 → refusal; facts=0 → correct answer, twice). A new `agents/knowledge/fact_relevance.py` keeps an unslotted dump only where it overlaps the question, leaves slotted lookups untouched, and degrades to keeping everything when it has nothing to filter on. On the live corpus: 15 → 0 facts for the two failing questions, and 15 → **14** for a question the dump genuinely answers. When a structured-only dump is emptied, the P1-6 fallback now runs a semantic search instead. Suite 489 → **506 passing**.

**2026-09-06 — F1, the turn budget.** The ladder bounded the Knowledge node and nothing else, but a turn is `classify + knowledge node + checkpointing` — each honouring its own budget while the total reached ~76s under a 60s client. `ChatService.handle_message_turn` now enforces a 52s `TURN_BUDGET_S` and genuinely **cancels** the graph, so the server stops working instead of finishing an answer nobody will receive. The node budget is derived from what the turn has left (45s → 37s) rather than declared independently, classification's retry loop is bounded by wall clock rather than attempt count (~31s → 12s), and `assert_ladder_is_consistent()` now checks the **sum** of the server-side rungs — the pairwise check is what passed this configuration. Worst-case server turn: **52s, down from ~76s**. Verified by booting a real server with a setting the old check accepted and watching it refuse. Suite 476 → **489 passing**.

**2026-09-06 — F2 / F3, from the live UI test (see `test.md`).** Driving the real chat UI in a browser found three defects the test suite could not — all three are now fixed.

- **F3 — failures are no longer silent.** The Supervisor's classification node and its Knowledge adapter both caught every exception and logged *nothing*; the entire diagnostic for a failed turn was `"error": "error"`. Both now log the exception with a traceback, and a provider rate limit is detected through the `__cause__` chain and told apart from a defect — labelled `rate_limited` and worded as a wait rather than a breakage. Verified live by forcing each failure through the running server.
- **F2 — per-stage LLM timeouts.** One 15s ceiling for every call sat right on top of the answer stage's measured range (1.3–13.6s), so healthy generations were cut off as failures. Now `short` (10s/22s) for classify/rewrite/extract and `answer` (30s/38s) for generation, with `assert_ladder_is_consistent()` rewritten to enforce that no single stage can consume the node's whole budget. Found alongside: **the Anthropic provider stored a `timeout` it never passed to the SDK** — the same defect as the Supervisor's Groq client, in a second place.

Suite 443 → **476 passing, fully green** (the Playwright browser is now installed, so the 1 failure and 4 errors that had persisted all along are gone).

**Also measured, and the reason F1 matters:** retrieval costs **0.14–0.24s** and is not the bottleneck — the four sequential LLM calls are. Median turn latency in the browser was **35s**, with 4 of 12 turns over 45s. Full detail in `test.md`.

**2026-09-06 — P1-6 structured-only fallback.** A structured-only lookup that finds nothing now retries semantically instead of returning an empty result. The defect was in the *graph topology*, not in any module: every unit test passed while the compiled graph silently never called vector search. Verified against the live database — the query that exposed it now retrieves the answering chunk and carries it into the prompt with citations. Suite 427 → **443 passing**.

**2026-09-06 — P2-2 → P2-6, the whole quality tier.**

- **P2-2 retrieval tuning, measured rather than guessed.** The BGE query instruction is applied to queries only (index-time stays bare, so no re-embedding). A new golden set and `scripts/calibrate_retrieval.py` measure both changes against the live corpus: the instruction moves **recall@1 from 76.9% to 92.3%** and MRR from 0.885 to 0.955. The same measurement showed the shipped `similarity_threshold = 0.70` kept the correct source for only **5 of 26** answerable questions, usually while it sat at rank 1 — so the floor is now **0.50**, and a new **relative score margin** (0.12) does the discriminating: it keeps the correct source for 26/26 while halving the mean result set.
- **P2-3 partial-commit rollback.** A failed ingestion now rolls back before recording `FAILED`, instead of committing the chunks it had staged.
- **P2-4 timeout ladder.** One `timeouts.py` declares client 60s > node 45s > completion 25s > call 15s, refuses to start on an inverted ladder, and is enforced by a test that also checks the frontend literal. Fixed along the way: the Supervisor's Groq client stored a `timeout` it never passed to the API, so classification had **no socket deadline at all**.
- **P2-5 durable state.** Ticket idempotency and crawl-discovery review moved out of per-process dicts into the database (migration `3d6f8b2c17ae`), where a second instance or a restart can see them. Both in-memory structures are now bounded caches rather than the guarantee.
- **P2-6 hygiene.** `echo`, `store_new.py`, the unregistered `api/chat.py`, the empty `auth/jwt.py` and the committed crawl artefact are deleted; the crawler's committed debug values are restored; the README is a real one.

Suite 364 → **427 passing**. Verified against the live stack; see §2.

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

**Current, after every P0-1, P0-2, P0-4, P1, P2, P1-6, F1–F7 and F9 fix:**
```
cd backend && env -u PYTHONPATH ./.venv/bin/python -m pytest -q
→ 638 passed, 2 skipped in 59.63s
```

Collected: 640 tests, **no failures and no errors**. The runtime also dropped from ~131s to ~58s: the two opt-in tests that were making real network calls on every run now skip correctly. The 1 failure and 4 errors that had persisted through every earlier report were all the same missing dependency — a Playwright browser, installed with `uv run playwright install chromium`. Nothing in the suite is non-deterministic.

### Verified against the live stack

The P1-6, P2 and F1-F7 work was checked against the running Docker stack, not only the test suite. The container was rebuilt after the F-series so it serves the current code; the frontend is bind-mounted, so its changes were already live.

**Full chain, end to end** (the question that originally failed at three separate layers):

> *"What was Alpinist Studios called before, and where is it based?"*
> → classified `DOMAIN_REQUEST` (F6) → `retrieval strategy=hybrid` (F5) → retrieval returns `soani-tech-is-now-alpinist-studios` → streamed as `accepted` + 3 stages + result (F7)
> → **"Alpinist Studios was previously called Soani Tech, and the company is based in Kathmandu, Nepal.[2]"**

Earlier detail:

| Check | Result |
|---|---|
| Migration `3d6f8b2c17ae` applied | `crawl_discovery` created with its `expires_at` index; `ticket.idempotency_key` added with `uq_ticket_idempotency_key` |
| Backend rebuilt and healthy | `GET /health` → `{"status":"ok"}` |
| Live retrieval config | threshold `0.5`, margin `0.12`, query instruction `'Represent this sentence for searching relevant passages: '` — read out of the running container |
| A question the old floor rejected | *"What are the payment terms?"* (top score 0.560) → correct answer, cited to `pricing.pdf` |
| Another | *"How does an engagement begin?"* (0.535) → correct answer, cited to `company_overview.pdf` |
| The answer-bearing chunk for a third | retrieved at **0.519** — above the new 0.50 floor, impossible under the old 0.70 one |
| That third question, after P1-6 | *"Does the company support remote or hybrid work?"* → retrieval returns **4 chunks (was 0)**, including the answering one at 0.519, and they reach the prompt with 4 citations |
| P1-6 regression check | reverting the new graph edge leaves all 14 unit tests passing and fails only the 2 compiled-graph tests — with the customer-visible symptom, *"No relevant documentation was found for this query."* |

**A note on measurement conditions, and a caveat on the last row.** Verification burned through the Groq free-tier budget: first the per-minute limit (the ingestion worker was draining a job backlog against the same quota — stopping it fixed that), and eventually the **daily** 200 000-token limit. The last row above was therefore verified through the real compiled graph against the real database with only the three LLM stages stubbed; the final generation step could not be re-run. Retrieval is what P1-6 changed, and retrieval is what was measured — but that is one step short of a full end-to-end chat turn, and it should be re-run once the quota resets.

Worth knowing generally: **ingestion and chat share one token budget**, so a backlog-draining worker can make the assistant look broken.

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
   │  POST /chat/stream (SSE)   POST /chat   GET /graph/*   POST /ingest/*
   ▼
 FastAPI  (main.py — also serves the frontend as StaticFiles at "/")
   │
   ├── ChatService  ── one turn, bounded by TURN_BUDGET_S (52s)
   │     │              buffered (/chat) and streamed (/chat/stream) share
   │     │              _prepare_turn / _finalize_turn, so they cannot drift
   │     ▼
   │   Supervisor LangGraph  (Postgres checkpointer)
   │     ├── classify_and_route   (Groq / Gemini / stub)
   │     │     scope comes from CorpusScope — the live document titles,
   │     │     refreshed on a TTL, not a hand-written paragraph
   │     ├── knowledge_agent  ──► Knowledge LangGraph
   │     │     rewrite → extract → route ──┬─► structured_lookup  (EAV)
   │     │                                 └─► vector_search      (pgvector)
   │     │        both arms run concurrently whenever an entity resolves;
   │     │        facts are relevance-filtered, chunks are never deleted
   │     │     → rank → dedupe → context → prompt → llm
   │     ├── ticket_agent  (interrupt() × 2 → TicketStore → SMTP)
   │     └── assemble_response
   │
   ├── /graph/*   read-only EAV graph browser
   └── /ingest/*  upload | crawl | discover | confirm  → 202 Accepted
                    └─► register_document_version → MinIO + job row
                                                          │
 worker service (scripts/run_worker.py) ◄─────────────────┘ claims the job
   └─► run_ingestion: fetch → Tika → chunk+embed
        → EAV extraction (Groq) → persist → cutover
```

Every Supervisor node is wrapped by `node_logging.log_node`: off at the
default `INFO`, and free text redacted to a shape summary even at `DEBUG`
unless `LOG_PII=true` (P0-4).

**Stack:** Python 3.12, FastAPI, LangGraph 1.x, SQLAlchemy 2 (async, psycopg3), Alembic, Postgres 16 + pgvector, MinIO, Apache Tika, Playwright, `sentence-transformers` (BAAI/bge-base-en-v1.5, 768-dim), Groq (`openai/gpt-oss-120b`) as the live LLM.

---

## 4. What is done

### 4.1 Database schema — complete
`backend/src/ai_customer_assistant/db/models.py`, 9 Alembic migrations in a clean linear chain, single head (`0001 → 11160c9078cc → 0003 → 1e4beb1b8d68 → 2a1c9f0e45d7 → 4c2d8a1f9e0b → 7b3e5c1a9d42 → 9f1a2b7c4e08 → 3d6f8b2c17ae`).

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
| **Authentication / authorisation** | Every endpoint is anonymous. The 0-byte `auth/jwt.py` placeholder was deleted in P2-6 — an empty file was not a plan; P0-3 adds the real module. |
| **Admin API** | `ingestion/storage/api.py:62-83` — all three dependencies `raise NotImplementedError`; the router is deliberately not registered (`main.py:78-80`). |
| **Ticket status lookup** | Routed away at classification time with a hardcoded "not available yet" string (`routing.py:_CHECK_STATUS_UNAVAILABLE`). No read path against the `ticket` table. |
| **Prompt management API** | Frontend edits never reach the server. |
| **Typed settings** | `config.py` now loads the environment (P0-2), but modules still read `os.environ` directly rather than a typed settings object. `agents/knowledge/config.py` shows the pattern to follow. |
| **CHANGELOG** | `CHANGELOG.md` is empty. (`README.md` was written in P2-6.) |
| **CI** | No `.github/`, no lint config, no formatter config, no coverage gate. |
| ~~**Worker in Docker**~~ | **Done (P1-5)** — a `worker` service now runs `scripts/run_worker.py`; the API only enqueues. |
| **Observability** | No structured logging, no metrics, no tracing. `trace_id` is generated and threaded but never actually logged anywhere. |
| ~~**Streaming responses**~~ | **Done (F7)** — `POST /chat/stream` reports progress from ~50ms; `POST /chat` is unchanged as the fallback. |
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

**Location:** `main.py:47-52` (the `auth/` package was removed in P2-6; this must create it)

```python
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

Anyone who can reach the port can: run unlimited LLM-billed chat turns, upload arbitrary documents into the knowledge base, trigger crawls of arbitrary URLs (a **server-side request forgery** vector — `/ingest/crawl` fetches any URL the caller names, including `http://169.254.169.254/` and internal hosts), read the entire knowledge graph, and create tickets that send email.

`_uploaded_by()` hardcodes the service-account UUID for every ingestion, so there is no attribution either.

**Fix (in order):**
1. Create `auth/jwt.py` (issue + verify), add a `get_current_user` dependency.
2. Split routers: public = `/chat`, `/health`; authenticated = `/ingest/*`, `/graph/*`; admin-only = the future `/admin/*`.
3. Replace `allow_origins=["*"]` with an env-driven allowlist.
4. Add an SSRF guard on `/ingest/crawl`: block private/loopback/link-local IP ranges after DNS resolution, and enforce a domain allowlist.
5. Add per-IP rate limiting on `/chat` and `/ingest/*`.
6. Set `_uploaded_by()` from the authenticated user.

---

### ✅ P0-4 — Full conversation state was printed to stdout — **FIXED 2026-09-06**

**Was:** `agents/supervisor/graph.py:56-79` (`_log_node`)

Every Supervisor node entry and exit did `print(json.dumps(state, indent=2))`. On a ticket turn that put the customer's raw message, the entire conversation history, and — inside the confirmation text — **their email address** on stdout. Unconditional: no log level, no environment guard, and it ran in the Docker image, so anyone with access to the container logs had the transcript.

**Three problems, of which privacy was only the first.** It could not be turned off — there was no level to raise and no flag to unset. `print()` bypasses logging entirely, ignoring handlers, formatting and `LOG_LEVEL`, and under `PYTHONUNBUFFERED=1` it is a synchronous write per node. And it serialised the whole state to indented JSON *before* deciding anything, so the cost was paid whether or not anyone was reading.

**Fixed** in a new `agents/supervisor/node_logging.py`:

| | |
|---|---|
| Emits at `DEBUG` through the logging module | off under the default `INFO`, obeys `LOG_LEVEL` like everything else |
| Checks `isEnabledFor(DEBUG)` **before** serialising | a disabled trace costs an integer comparison, not a JSON dump |
| Redacts free text even when tracing is on | routing stays debuggable without reproducing what anyone said |
| `LOG_PII=true` opts back in | deliberate, visible, and the only way |

**Why redact rather than just gate.** A pure on/off flag fails the moment someone needs to debug a live routing problem: the only way to see which branch a turn took would be to also print the customer's message. So the default keeps every routing field and replaces free text with a shape summary:

```
{
  "user_message": "<redacted str, 46 chars>",
  "conversation_history": "<redacted list, 1 items>",
  "request_category": "DOMAIN_REQUEST",
  "domain_confidence": 0.95,
  "intent": "CREATE_TICKET",
  "next_agent": "TICKET_AGENT",
  "final_response": "<redacted str, 67 chars>"
}
```

**The email drove the design.** It never arrives in a field called `email` — it is embedded in the ticket confirmation prose, so `response` and `final_response` are redacted too. Trusting a field name to be honest about what it holds is what would have missed it.

**Verified live.** A turn carrying `my card 4111-1111-1111-1111 was double charged, email me at alice@example.com` produced **zero** occurrences of the card number, the address or the message text in the container logs — and log volume per turn fell from hundreds of lines to 18.

**Files changed:** `agents/supervisor/node_logging.py` (**new**), `agents/supervisor/graph.py`, `tests/agents/supervisor_agent_test/test_node_logging.py` (**new**, 29 tests).

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

**Retrieval quality since then:** P2-2 (the missing BGE query instruction and the uncalibrated 0.70 threshold) is now fixed and measured, and **P1-6** — where the router could skip this path entirely — is fixed too. See both entries below.

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

### ✅ P1-6 — A confident extraction silently disabled semantic search — **FIXED 2026-09-06**

**Was:** `agents/knowledge/graph.py`, `agents/knowledge/hybrid.py`, `agents/knowledge/nodes.py`

Found while verifying P2-2, and worse than the thresholds it was hiding behind.

`decide_strategy` routes to **structured-only** whenever the extractor named an entity type, asked for a specific slot, and *was confident*. When that lookup then found nothing, retrieval ended with zero results — and **there was no fallback to vector search**. Observed live, with everything else working correctly:

```
query:       "Does the company support remote or hybrid work?"
extraction:  StructuredQuery(entity_type='Policy', relation_type='supports', confidence=0.85)
strategy:    structured        (0.85 clears extraction_confidence_threshold 0.55)
structured:  0 facts
vector:      never ran
answer:      "I'm sorry, I don't have information on whether the company
              supports remote or hybrid work."
```

Vector search, run directly against the same database, returns the chunk that answers it at **0.519**.

**What was *not* wrong.** This matters for understanding the fix. `Policy` is a legitimate entity type in the shared ontology — the extractor did not hallucinate it, and an ontology-membership check would not have caught this. There simply are no `Policy` entities in this graph. So the defect is structural: **a confident extraction is allowed to switch off the retrieval path that works**, and the more certain the extractor sounds, the more often it fires.

*(This corrects the first write-up of P1-6, which said `'Policy'` "resolves to nothing in the graph" and suggested pairing the fix with an ontology check. The type resolves fine; it has no instances.)*

**The fix.** A structured-only lookup that comes up empty now falls back to vector search — whatever the reason it came up empty (unknown entity, no instances, missing attribute, ambiguity). The fallback costs one query and can only add information.

| Piece | Role |
|---|---|
| `hybrid.should_fall_back_to_vector(strategy, facts)` | The single definition of the rule: structured-only **and** nothing found. Both entry points call it, so they cannot drift. |
| `nodes.make_structured_fallback_edge(config=...)` | The conditional edge the compiled graph traverses: `structured_lookup → vector_search` when the rule holds, `→ rank` otherwise. |
| `hybrid._structured_only` | The same behaviour for the directly-callable orchestrator, including degrading `EntityNotFoundError` to "found nothing" so both doors behave alike. |

The hybrid strategy reaches the same edge and always routes to `rank`, because its vector search is already running in parallel — so the fallback can never double-run it.

**Why the tests are shaped the way they are.** The bug lived in the *topology*, not in any module: `hybrid_retrieve`, `decide_strategy`, `structured_lookup` and `vector_search` were each individually correct. `tests/agents/knowledge/test_structured_fallback.py` (**new**, 16 tests) therefore drives the **real compiled graph**, not just the helpers. Checked by reverting the edge: the 14 unit tests still passed, and only the two graph tests failed — with exactly the symptom the customer saw, `"No relevant documentation was found for this query."`

**Verified on live data** (the graph and database are real; only the three LLM stages are stubbed, because the Groq daily token quota was exhausted at the time):

```
strategy:          structured
structured facts:  0
retrieved chunks:  4     ← was 0
   0.534 idx=6                    compnay_vision.pdf
   0.519 idx=3  ← answers it      compnay_vision.pdf
   0.501 idx=5                    compnay_vision.pdf
   0.500 idx=0                    compnay_vision.pdf
reached the prompt: yes, with 4 citations available
```

**Files changed:** `agents/knowledge/hybrid.py`, `agents/knowledge/nodes.py`, `agents/knowledge/graph.py`, `tests/agents/knowledge/test_structured_fallback.py` (**new**).

**Small thing left behind.** `KnowledgeAgentState.retrieval_strategy` is declared and documented as "set by the decide_strategy conditional edge" but is never actually populated — LangGraph conditional edges cannot write state. Both edges recompute the (pure, cheap) decision instead. Populating it would need a small `decide_strategy` node and would make the chosen strategy visible in traces; worth doing when observability is added (§7.5).

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

### ✅ P2-2 — Retrieval quality was untuned — **FIXED 2026-09-06**

**Was:** `services/embeddings.py`, `agents/knowledge/constants.py`, `agents/knowledge/vector_search.py`

Two defects that had to be fixed together, because fixing either alone makes the other worse.

**1. The query side had no instruction.** `bge-*-en-v1.5` is trained for *asymmetric* retrieval: passages are encoded bare, queries carry a fixed instruction. This project encoded both sides bare, so every query was embedded under a convention the weights were never trained on. Index-time was already correct, which is why the fix needs **no re-embedding** — only the query side changes, and it changes toward what the model expects.

**2. `similarity_threshold = 0.70` was chosen with nothing behind it.** An absolute floor is genuinely hard to pick by intuition here, because normalised BGE similarity is compressed: unrelated English text does not score near zero, it scores in the same 0.6–0.7 band as weakly-relevant text.

**What the measurement showed.** `backend/scripts/calibrate_retrieval.py` and a golden set of 26 answerable + 10 unanswerable questions (`backend/tests/data/golden_retrieval.json`), run against the live 27-chunk corpus:

| | recall@1 | recall@8 | MRR |
|---|---|---|---|
| Without the query instruction (previous behaviour) | 76.9% | 100% | 0.885 |
| **With it** | **92.3%** | 100% | **0.955** |

And the floor, with the instruction applied:

| threshold | answerable questions kept | unanswerable leaked |
|---|---|---|
| 0.50 | **25/26** | 2/10 |
| 0.55 | 22/26 | 1/10 |
| 0.60 | 15/26 | 0/10 |
| **0.70 (what shipped)** | **5/26** | 0/10 |

**The 0.70 floor was rejecting the correct source for 21 of 26 answerable questions — and in most of those the correct document was already ranked first.** The system had the answer and refused to use it. Note also that the instruction *lowers* absolute scores (median top score 0.681 → 0.637) while improving ranking, so it could not have been adopted without moving the floor.

**What changed:**

| Change | Detail |
|---|---|
| Query instruction | `SharedEmbeddings` prepends it to queries only. Resolved per model family — `bge-*-en-v1.5` gets BAAI's documented string, anything else (`bge-m3`, `e5`) gets nothing, because the wrong prefix is worse than none. `EMBEDDING_QUERY_INSTRUCTION` overrides; empty disables. |
| Threshold | 0.70 → **0.50**, from the table above. It is now a *sanity* floor — it rejects "what is the recipe for sourdough bread" (0.388), not near-misses. |
| **Relative score margin** (new) | Drops results scoring more than 0.12 below the query's own best hit. This is the signal an absolute cutoff throws away: a chunk at 0.66 is noise when the top hit is 0.83 and relevant when the top hit is 0.71. Swept over the golden set, 0.12 keeps the correct source for **26/26** while cutting the mean result set from 8 chunks to 4.5. |
| Both ranking paths | The margin is applied identically to the pgvector and Python paths, so they stay equivalent by construction. |

**Why prefer recall.** The two errors are not symmetrical. An irrelevant chunk that gets through still has to survive the answer prompt and the groundedness check. A rejected chunk has no second chance — the turn simply fails and the customer is told nothing is known about a question the corpus answers.

**Files changed:** `services/embeddings.py`, `agents/knowledge/constants.py`, `agents/knowledge/config.py`, `agents/knowledge/vector_search.py`, `scripts/calibrate_retrieval.py` (**new**), `tests/data/golden_retrieval.json` (**new**), `tests/agents/knowledge/test_retrieval_tuning.py` (**new**, 20 tests).

**Honest limits.** The golden set is sized to a 27-chunk corpus. It is enough to place the floor and catch a regression; it is **not** enough to justify fine claims about small differences in recall. Re-run the script after any corpus or model change — the numbers are specific to both.

---

### ✅ P2-3 — Failed ingestion could commit partial data — **FIXED 2026-09-06**

**Was:** `ingestion/pipeline.py`

`_stage_persist` wrote chunks and EAV rows with `add`/`flush` and no commit. On failure the `Err` branch called `mark_version_status(..., FAILED)` — which *does* commit — carrying the partial writes into the database alongside the failure.

**Fix:** `await session.rollback()` before recording `FAILED`. Safe because every row the handler still needs (job, source, version) was committed before the pipeline started, so the status update simply re-reads in a fresh transaction.

`tests/ingestion/test_partial_commit_rollback.py` (**new**, 3 tests) asserts the *ordering* — `flush → rollback → commit` — because a rollback after the commit would be a no-op on already-durable rows.

---

### ✅ P2-4 — Frontend/backend timeout mismatch — **FIXED 2026-09-06**

**Was:** `frontend/src/pages/chat.js`, `agents/supervisor/graph.py`, `agents/knowledge/providers.py`, `agents/supervisor/llm_client.py`

The browser gave up after 60s, the Knowledge node was allowed 120, and a rate-limited Groq call could sleep up to 300. A 60–120s turn was abandoned in the browser while the server finished it and checkpointed it: the customer saw "Request timed out" and their next message resumed from a state they had never seen. Nothing logged an error, because every layer behaved exactly as configured.

**Fix:** one `backend/src/ai_customer_assistant/timeouts.py` declaring the ladder, each rung strictly inside the one waiting on it:

```
client 60s  >  knowledge node 45s  >  one LLM completion 25s  >  one HTTP call 15s
```

It raises at import on an inverted ladder — a misconfiguration here produces no runtime error, so a loud failure at boot is the only cheap moment to notice it.

**Two related defects fixed alongside:**

- **The Supervisor's Groq client stored a `timeout` it never passed to the API call.** Classification had no socket deadline whatsoever; a stalled connection held the chat turn open indefinitely.
- **The Groq retry budget is now wall-clock, not a per-sleep clamp.** Clamping each sleep at 300s still allowed several clamped sleeps in a row. The loop now stops as soon as the next attempt could not finish within what is left.

`tests/test_timeout_ladder.py` (**new**, now 19 tests) covers the ordering, the boot-time refusal, the retry budget, the classify deadline, and — by reading `chat.js` — that the frontend literal still agrees.

**Refined 2026-09-06 (F2).** Live testing showed one timeout for every LLM call was too blunt: the answer stage measures 1.3–13.6s against classify's 1.1s, so a shared 15s ceiling cut off healthy generations. The two innermost rungs are now per stage — see `test.md` F2.

**Completed 2026-09-06 (F1).** The original fix bounded the Knowledge *node*, not the whole turn, so `classify + node + checkpointing` could still overrun the client. The turn is now the budgeted unit, the node budget is derived from it, and the consistency check tests the sum rather than each pair — see `test.md` F1.

---

### ✅ P2-5 — In-memory state defeated horizontal scaling — **FIXED 2026-09-06**

**Was:** `api/ingest.py`, `agents/ticket_agent/store.py`

Both problems had the same shape: a documented guarantee that was only ever true inside one process.

| State | What actually happened | Now |
|---|---|---|
| `_discovery_cache` | Discovery and confirmation are two HTTP requests with a human review between them. With two instances the confirm was roughly a coin flip, and its 404 said *"unknown or expired discovery_id"* — blaming a TTL that had not elapsed. A restart did the same. | `crawl_discovery` table with `expires_at`; expired rows swept on lookup. The `CrawlConfig` is stored with the result, so `confirm` cannot be made to crawl under wider settings than discovery ran with. |
| `TicketStore._by_key` | Two instances each held their own empty dict, so a retried request booked a **second ticket and sent a second confirmation email**. So did one instance restarted between the request and its retry. | `ticket.idempotency_key` + `uq_ticket_idempotency_key`. The insert catches the conflict, reads back the row that won, and returns it — **without notifying**, so the loser of the race sends no second email. |
| `TicketStore.rows` | Grew without bound for the process lifetime. | Bounded deque; an observability window, never the source of truth. |
| `next_sequence` | Counted the in-memory map, so a restarted process restarted the ordinal at zero and reissued a key an earlier ticket already held — the customer's *new* ticket would be deduplicated against their previous one and they would see the wrong confirmation. | Counted in the database, with `autoescape` so a `thread_id` containing `%` or `_` is not read as a LIKE wildcard. |

Migration `3d6f8b2c17ae` is additive: nothing dropped, no row changed, exact downgrade. `_idempotency_key` became a coroutine to await the database-backed ordinal, and still accepts a synchronous `next_sequence` so hand-rolled test fakes keep working.

**A latent bug surfaced by the tests.** `Ticket.ticket_id` is a `str` while the column is `UUID`. psycopg happens to accept the string, so Postgres never complained — but SQLAlchemy's portable `Uuid` type calls `.hex` on it, so the same insert failed on any other dialect. The store now coerces explicitly rather than leaving a driver coincidence load-bearing.

**Files changed:** `db/models.py`, `agents/ticket_agent/store.py`, `agents/supervisor/agents_wiring.py`, `api/ingest.py`, `alembic/versions/3d6f8b2c17ae_durable_state_for_scale_out.py` (**new**), `tests/agents/ticket_agent/test_durable_idempotency.py` (**new**, 16 tests), `tests/api/test_crawl_discovery_persistence.py` (**new**, 13 tests).

---

### ✅ P2-6 — Repository hygiene — **FIXED 2026-09-06**

| Item | Action |
|---|---|
| `echo` (repo root) | Deleted. Contained a stray AI-assistant summary: *"Task complete. The ticket agent now has…"*. |
| `backend/store_new.py` | Deleted — a 190-line dead near-duplicate of the real store. |
| `api/chat.py` | Deleted — an entire unused router, never registered; the explanatory note in `main.py` went with it. |
| `auth/jwt.py` | Deleted. It was 0 bytes; an empty file is not a plan. **P0-3 will add a real module.** |
| `output/docs.md` | Deleted and `output/` gitignored — a tracked crawl artefact from `example.com`. |
| `frontend/app/.vite/deps_temp_*` | Deleted and `**/.vite/` gitignored. |
| **Committed debug values** | `CrawlConfig.request_timeout` 520.0 → **15.0** and `max_pages` 5 → **50**, the values from before commit `02b41a5`. A 520-second per-request timeout stalls a crawl for nearly nine minutes on one unresponsive page; `max_pages=5` silently truncated every site crawl to five pages. |
| `README.md` | Rewritten. **The report was wrong about this one**: it was not the one-line `# AI Customer Assistant`, it contained a single stray absolute path. It is now real setup documentation — requirements, first run, the two-`.env` split, every make target, ingestion, tests, layout, and an explicit "there is no authentication" warning. |
| `.pytest_cache` | Removed from disk (already gitignored). |
| Branches | **Deliberately not touched.** 13 local + 24 remote branches remain. Deleting branches is irreversible and is the user's call, not a hygiene sweep's. |

`config.py` is no longer 0 bytes — P0-2 gave it `load_env()`.

---

## 7. Recommended improvements

### 7.1 Correctness & safety (do first)
1. ~~Fix the ticket flow (P0-1).~~ **Done 2026-09-05** — 7 regressions cleared, 11 tests added.
2. ~~Remove import-time `load_dotenv()` (P0-2).~~ **Done 2026-09-06**
3. Implement JWT auth + CORS allowlist + SSRF guard on the crawler (P0-3).
4. ~~Replace `print()` node logging with redacted structured logging (P0-4).~~ **Done 2026-09-06**
5. ~~Fall back to vector search when a structured-only lookup returns nothing (P1-6).~~ **Done 2026-09-06**
6. Mark DB/Playwright tests with `@pytest.mark.integration` and add a `-m "not integration"` default so the unit suite is green on a clean checkout.

### 7.2 Performance & scale
6. ~~pgvector `<=>` + HNSW index (P1-1).~~ **Done 2026-09-05** — see the P1-1 entry.
7. ~~Non-blocking LLM calls (P1-2).~~ **Done 2026-09-05**
8. ~~Remove the `?` splitter (P1-3).~~ **Done 2026-09-05**
9. ~~Consolidate to one database engine with explicit pool sizing (P1-4).~~ **Done 2026-09-05**
10. ~~Move ingestion out of the API into the worker; add it to `docker-compose` (P1-5).~~ **Done 2026-09-05**
11. Add a Redis cache for repeated identical queries (embedding + answer) — support traffic is heavily repetitive.

### 7.3 Quality
12. ~~Unify the ontologies with a drift test (P2-1).~~ **Done 2026-09-06**
13. ~~Build a golden Q&A evaluation set; calibrate `similarity_threshold` against it (P2-2).~~ **Done 2026-09-06** — 26+10 questions, `scripts/calibrate_retrieval.py`; threshold 0.70 → 0.50, relative margin added. Grow the set as the corpus grows.
13b. ~~Stop unfiltered structured-fact dumps diluting the answer prompt (F4).~~ **Done 2026-09-06** — `agents/knowledge/fact_relevance.py`.
14. Add a re-ranking stage (cross-encoder) — currently ranking is a weighted score with no second pass.
14b. ~~Populate `KnowledgeAgentState.retrieval_strategy`.~~ **Done 2026-09-06 (F5)** — a `route` node writes it; both edges read that one value.
15. ~~Wrap ingestion in a proper transaction boundary (P2-3).~~ **Done 2026-09-06**
16. Add `ruff` + `mypy` and a GitHub Actions workflow: lint → type-check → unit tests → coverage gate.

### 7.4 Features
17. **Ticket status lookup** — the `ticket` table exists and the `CHECK_TICKET_STATUS` intent is already classified; only the read path is missing. Cheapest remaining MVP feature.
18. **Admin API** — implement the three `NotImplementedError` dependencies in `ingestion/storage/api.py`, add `/admin/sources|jobs|stats|tickets`, and register the router. The frontend page is already written and waiting.
19. **Prompt management endpoint** so the Prompt page's edits persist server-side and are versioned.
20. ~~**Streaming `/chat`** via SSE.~~ **Done 2026-09-06 (F7)** — `POST /chat/stream`; the buffered endpoint remains as the fallback.
21. **Ticket lifecycle** — `updated_at`, `resolved_at`, assignee, a `thread_id` FK linking a ticket back to its conversation, and inbound email replies.

### 7.5 Operations
22. Structured JSON logging that actually emits the `trace_id` already threaded through every node. (Log *levels* were fixed with F5 — `logging_config.configure_logging()`; the remaining work is structure and the trace id.)
23. Prometheus metrics: chat latency by stage, retrieval hit rate, LLM token spend, job queue depth.
24. `/health` should check Postgres, MinIO and Tika — it currently returns `{"status": "ok"}` unconditionally.
25. ~~A stale-job reaper.~~ **Done 2026-09-05** with P1-5.
26. Move secrets to a secret manager; `backend/.env` currently holds a live `GROQ_API_KEY` and SMTP password in plaintext on disk (correctly gitignored, but not protected).
27. ~~Write the README.~~ **Done 2026-09-06** with P2-6 — requirements, first run, the two-`.env` split, make targets, ingestion, tests and layout. An architecture diagram is still missing.

---

## 8. Quick wins (under an hour each, high value)

- [x] ~~Add the missing `f` to the email subject~~ — **done** (P0-1)
- [x] ~~`git rm echo backend/store_new.py backend/src/ai_customer_assistant/api/chat.py`~~ — **done** (P2-6)
- [x] ~~Delete the `?` splitting block~~ — **done** (P1-3)
- [x] ~~Guard `_log_node` behind an environment check~~ — **done** (P0-4), and it redacts rather than merely gating
- [ ] `allow_origins` from an env var — `main.py:49` (P0-3)
- [x] ~~Revert `CrawlConfig.request_timeout` to 15.0 and `max_pages` to 50~~ — **done** (P2-6)
- [x] ~~Add `output/`, `frontend/app/.vite/` to `.gitignore`~~ — **done** (P2-6)
- [x] ~~Use `self._session_factory` in `TicketStore.create_ticket`~~ — **done** (P0-1)
- [ ] Make `/health` check the database
- [x] ~~Write a real README~~ — **done** (P2-6)
- [ ] Log the swallowed exception in `classify_and_route` — `node.py:79` catches every failure and returns the safe fallback **without logging anything**, which is why a Groq 429 during verification looked like an unexplained "something went wrong". One `logger.warning(..., exc_info=True)`.

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
make chat                    # open the chat UI (port from APP_PORT in ./.env)
make verify                  # list knowledge sources
```

**Apply migrations from the host** (the compose Postgres is published on 5433):
```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH PYTHONPATH=src/ai_customer_assistant ./.venv/bin/python -m alembic upgrade head
```

**Codebase size:** backend `src` 17 005 lines · tests 9 116 lines · frontend 3 892 lines. The test suite roughly doubled over this work — 268 → 642 passing.

**Largest modules:** `frontend/src/pages/graph.js` (930) · `ontology/vocabulary.py` (596) · `frontend/src/pages/ingest.js` (509) · `agents/knowledge/structured_lookup.py` (455) · `api/ingest.py` (~470).

### Configuration

Everything below has a working default; none of it needs setting to run the
stack. Listed because most of it was added during this work and appears in no
other document.

**Timeout ladder** (`timeouts.py` — the arithmetic is checked at import and the
process refuses to start if it does not hold):

| Variable | Default | What it bounds |
|---|---|---|
| `CLIENT_REQUEST_TIMEOUT_S` | 60 | The browser's own limit; declared here as the reference point, and mirrored in `chat.js` |
| `TURN_BUDGET_S` | 52 | One whole `POST /chat`, enforced in `ChatService` |
| `CLASSIFY_BUDGET_S` | 12 | Classification including retries |
| `CHECKPOINT_HEADROOM_S` | 3 | Reading and writing the checkpoint |
| `KNOWLEDGE_NODE_TIMEOUT_S` | *derived* | Whatever the turn budget has left; an explicit value is accepted and validated |
| `LLM_SHORT_TIMEOUT_S` / `LLM_SHORT_RETRY_BUDGET_S` | 10 / 22 | One classify/rewrite/extract call, and that stage with retries |
| `LLM_ANSWER_TIMEOUT_S` / `LLM_ANSWER_RETRY_BUDGET_S` | 30 / 34 | The same for answer generation, which is legitimately an order of magnitude slower |

**Retrieval** (`KNOWLEDGE_AGENT_` prefix, `agents/knowledge/config.py`):

| Variable | Default | Notes |
|---|---|---|
| `KNOWLEDGE_AGENT_SIMILARITY_THRESHOLD` | 0.50 | Absolute floor. **Measured, not guessed** — see P2-2 before changing it |
| `KNOWLEDGE_AGENT_RELATIVE_SCORE_MARGIN` | 0.12 | How far below the best hit a chunk may score and survive |
| `KNOWLEDGE_AGENT_TOP_K` | 8 | Must not exceed `MAX_CONTEXT_CHUNKS` |
| `KNOWLEDGE_AGENT_MAX_CONTEXT_CHUNKS` / `MAX_STRUCTURED_FACTS` | 12 / 20 | Prompt budgets |
| `KNOWLEDGE_AGENT_EXTRACTION_CONFIDENCE_THRESHOLD` | 0.55 | No longer used for routing (F5); still gates extraction |
| `KNOWLEDGE_AGENT_LLM_PROVIDER` / `LLM_MODEL_NAME` / `REWRITE_MODEL_NAME` | anthropic / claude-sonnet-5 | Falls back to the stub when the credential is absent |
| `EMBEDDING_QUERY_INSTRUCTION` | *model-derived* | Overrides the BGE query prefix; set to empty to disable it |

**Logging and privacy:**

| Variable | Default | Notes |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Uvicorn leaves the root at WARNING, so this is what makes application logs appear at all |
| `LOG_PII` | unset (off) | Opt back in to un-redacted node traces. **Never set in a deployed process** — see P0-4 |

**Database pool** (`db/engine.py`): `DB_POOL_SIZE` (10), `DB_MAX_OVERFLOW` (5), `DB_POOL_TIMEOUT` (30), `DB_POOL_RECYCLE` (1800).

**Credentials and services:** `GROQ_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY`, `POSTGRES_*`, `MINIO_*`, `TIKA_BASE_URL`, `INGEST_DEFAULT_USER_ID`, `FRONTEND_DIR`.

**Re-run the retrieval calibration** after changing the corpus, the embedding model or the thresholds:
```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH ./.venv/bin/python scripts/calibrate_retrieval.py --detail
```

---

## 10. Verdict

The engineering *discipline* in this codebase is unusually good: consistent dependency injection, pure functions isolated from I/O, immutable data types, dispatch tables in place of conditional chains, and docstrings that explain *why* rather than *what*. Whoever wrote the Knowledge Agent and the ingestion pipeline knew what they were doing.

What is missing is the last mile. The system was built module-by-module against a plan document, and the seams show where the modules meet: the ticket flow half-migrated to async and broke, the two ontologies forked and drifted, four database engines accumulated, the timeout budgets contradicted each other across the client/server boundary, state that had to be shared was left in per-process dictionaries, and the retrieval graph had a branch with no way out. All of those are now fixed. What is still absent is the set of concerns no single module owns: **auth, logging and CI**.

A pattern worth naming, because it recurs and it is the hardest kind of defect to notice: **most of the bugs found here failed silently rather than loudly.** A threshold rejected four-fifths of answerable questions. An idempotency guarantee held only inside one process. A timeout abandoned requests the server went on to complete. A classifier swallowed every exception and returned a friendly apology. A retrieval branch skipped the only search that would have worked. In each case every component behaved exactly as written, so nothing errored and no log line appeared — the damage was only visible in the relationship *between* components, or by measuring the output against what it should have been.

P1-6 is the sharpest illustration, and the reason it is worth changing how this codebase is tested. Every module involved was individually correct and individually well tested; the defect was one missing edge in the graph that connects them. Unit tests could not have found it, and did not. The fixes that catch this class of bug are the ones that assert across boundaries: the golden set that measures retrieval end to end, the ladder assertion in `timeouts.py`, the ontology drift tests, and the compiled-graph tests added with P1-6.

**With the two P0 security items addressed (roughly one focused week), this becomes a genuinely deployable internal product.**

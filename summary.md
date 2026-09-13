1. PURPOSE

A retrieval-augmented customer-support assistant. A LangGraph supervisor classifies each inbound message and routes it to a RAG knowledge agent, a multi-turn ticket-creation agent, or a ticket-status lookup. Ingested documents (PDF/DOCX/Markdown uploads or Playwright website crawls) are indexed twice — as pgvector embeddings for semantic search and as an entity/attribute/value/relation graph for exact structured lookups — so factual questions can be answered from both at once.

2. STACK

Python 3.12; FastAPI + Uvicorn; LangGraph 1.2 / langchain-core; SQLAlchemy 2 (async) + Alembic; Postgres 16 via pgvector/pgvector:pg16, pgvector + HNSW (vector_cosine_ops, backend/alembic/versions/9f1a2b7c4e08_*.py); MinIO (object store); Apache Tika (apache/tika:latest-full) for extraction; sentence-transformers with BAAI/bge-base-en-v1.5, 768-dim (agents/knowledge/constants.py:127); Groq (openai/gpt-oss-120b), Anthropic (claude-sonnet-5) and Gemini (gemini-2.0-flash) providers plus a deterministic stub (agents/knowledge/providers.py:388-404); Playwright/Chromium + trafilatura for crawling; PyJWT + argon2-cffi for auth; langgraph-checkpoint-postgres (AsyncPostgresSaver) for conversation state; SMTP via stdlib smtplib (agents/ticket_agent/store.py:53). Frontend is zero-build vanilla JS (classic scripts, window.ACA namespace, hash router) — no framework. Docker Compose runs 5 services + a MinIO init container.

3. ARCHITECTURE

Two independent paths share one Postgres.

Ingestion (async, out-of-process): API enqueues only; a separate worker container drains a Postgres-backed queue claimed with FOR UPDATE SKIP LOCKED (ingestion/queue/repository.py:62), 2 concurrent lanes sharing one embedding model. Seven curried Result-returning stages (ingestion/pipeline.py:266-272): fetch bytes → Tika extract → load reuse checksums → chunk+embed → EAV extraction → persist → cutover. Chunking is strategy-dispatched by source type (structure-aware split at headings, then 500-token windows with 75 overlap; structured sources are one-row-one-chunk, and an unknown type is a hard error rather than a guess — chunk_embed/chunking.py). Cutover flips knowledge_source.current_version_id, so a failed re-ingest cannot half-replace a live document.

Query: POST /chat or /chat/stream → supervisor graph (5 nodes: classify → knowledge | ticket | ticket_status → assemble) → knowledge subgraph, 10 nodes: rewrite → extract → route → structured_lookup and/or vector_search → rank → deduplicate → build_context → build_prompt → llm. Non-obvious decisions, each documented in-code: route is a node not an edge because an edge cannot write state and the strategy choice had to be observable (agents/knowledge/graph.py:131); a structured_fallback conditional edge retries semantically when a confident extraction returns zero rows, a failure that previously reported success (graph.py:149); vector search deliberately does not use an ANN pre-filter CTE so the live-version join stays exact, accepting a sort (vector_search.py:30-43); the ontology was moved to a neutral ontology/ package because forked copies in ingestion and retrieval drifted silently (ontology/vocabulary.py).

4. HARD PARTS

1. Entity fragmentation. Keying entity identity on the model-chosen type split one real thing across rows ("Agile" as three entities holding 39/14/2 facts). Fix: identity is the normalized name alone, plus a one-shot idempotent reconcile.py that merges groups and repoints value/relation (both sides)/embedding_chunk/knowledge_source_entity_map FKs in a single transaction, with pure planners split out for unit testing.
2. Extraction under a rate-limited free tier. gpt-oss-120b degrades past ~2k chars, so chunks are split into 1800-char overlapping windows, batched 3 at a time, JSON-mode not tool-calling (which emitted one call/turn). Bounded on two axes — per-request client timeout plus a per-document 600s budget — after one document held the serial worker for 28 minutes. Throttling and refusal are separated at the except so the retry policy dispatches on a code; 429 cooldown capped at 60s because Groq's TPM bucket refills in a minute (ingestion/extraction/agent.py:74-99).
3. Retry/dead-letter semantics. A pure (failure_kind, attempts) → decision function with an allowlist of 5 transient kinds; everything unclassified is terminal by default, and DEAD_LETTER is distinct from FAILED (ingestion/queue/retry.py).
4. SSRF guard for user-supplied crawl URLs. Scheme allowlist, resolve all addresses and reject if any is private, optional domain allowlist, post-connection re-check, manual redirect following (3 hops max). The residual gap — httpx cannot pin a connection to the validated address without breaking cert verification — is documented rather than papered over (auth/ssrf.py).

5. COUNTABLE FACTS

- HTTP endpoints: 27 (26 router-decorated + GET /health). By router: chat 2, graph 5, ingest 4, admin 7, auth 5, users 1 (POST /auth/users), health 1.
- DB tables: 17 (db/models.py). Migrations: 15.
- Tests: 973 passed, 2 skipped (./.venv/bin/python -m pytest -q, 367s). 866 test functions across 76 files. No unit/integration split exists as a marker — 6 files self-skip without live Postgres or a Groq key; 13 use in-memory SQLite. Say "not present" for a formal split.
- LOC: backend src 21,725 (129 files); backend tests 14,325 (76 files); frontend 3,669 (16 files).
- Pipeline stages: 7. Supervisor graph nodes: 5. Knowledge subgraph nodes: 10.
- Ontology: 105 entity types, 28 relation types, 85 attributes.
- Upload formats: 3 (PDF, DOCX, Markdown — api/ingest.py:_UPLOAD_MIME_TO_FILE_TYPE).
- LLM providers: 4 (groq, anthropic, gemini, stub). Models trained: none — embedding model is pretrained, off the shelf.
- Retrieval golden set: 26 positives, 10 negatives (tests/data/golden_retrieval.json).
- Commits: 77, 2026-07-27 → 2026-09-11.

6. MEASURED RESULTS

Real, from status.md / test.md, produced by scripts/calibrate_retrieval.py against the live corpus:
- BGE query-instruction (queries only, no re-embedding): recall@1 76.9% → 92.3%, MRR 0.885 → 0.955.
- Similarity threshold 0.70 kept the correct source for only 5 of 26 answerable questions; lowered to 0.50 with a 0.12 relative score margin → 26/26, mean result set roughly halved.
- Per-node latency: vector_search 0.14–0.23s, structured_lookup 0.14–0.24s, rank/dedupe/context/prompt 0.00s, rewrite 0.52–1.94s, classify 1.1s, answer generation 1.26–13.63s.
- End-to-end: median 35s in browser, 4.1s fastest / 60.0s slowest (client abort), 4 of 12 turns over 45s. Streaming first stage event at 0.05s.
- Caveat stated in test.md:124: latency was not re-measured after the timeout-ladder change — the Groq daily quota was exhausted.

7. COMPLETENESS

Fully working (verified in code + tests): supervisor classification/routing; knowledge RAG subgraph incl. hybrid fan-out, vector fallback, ranking, dedupe, citations; ingestion pipeline end-to-end with retry/dead-letter and version cutover; Postgres job queue with SKIP LOCKED and 2 lanes; EAV graph write + structured lookup; pgvector search with live-version join; ticket creation, persistence and SMTP confirmation; ticket status lookup by id; authentication (argon2, JWT, HttpOnly cookies + bearer, refresh rotation with reuse detection), two roles, DB-backed rate limiting (5 logins/15min per IP and per account, 20 chat/min, 500 chat/day, 10 ingest/min — all three enforced at auth/dependencies.py:246-254); SSRF guard; admin read API (sources, jobs, stats, tickets) wired to a live frontend; LLM provider key storage encrypted under an AUTH_SECRET-derived key; durable LangGraph checkpointer; SSE streaming; crawl discover→review→confirm persisted in crawl_discovery with TTL.

Partially implemented: prompt management — frontend edits persist to localStorage only, no write endpoint (frontend/src/pages/prompt.js:4). Superseded facts are stored (value.superseded_at, value_provenance) but no temporal query exists. Observability — ingestion logs carry [<job>.<attempt>]; the chat path threads a trace_id through graph config but never sets the logging context, so its lines print -. Session management — "sign out everywhere" exists as a code path with no UI. Config — config.py loads env, but modules still read os.environ directly rather than a typed settings object. HNSW index exists but the planner does not use it under the version join.

Stubbed / not built: ingestion/storage/api.py — 3 dependencies raise NotImplementedError, router deliberately unregistered (main.py:141). No admin write surface: no source deletion, no re-index trigger, no ticket status change. No provider-key validation before save; no per-agent provider override. No CI, no .github/, no ruff/mypy/formatter config, no coverage gate. No notebooks. No structured logging, metrics or tracing.

Two documentation defects worth knowing before an interview: status.md §4.6 still calls the Admin page a stub, which the shipped api/admin.py and frontend/src/pages/admin.js contradict; and the README's "969 passing" is now 973.
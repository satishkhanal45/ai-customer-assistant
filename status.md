# AI Customer Assistant — Project Status Report

**Repository:** `/mnt/hdd/satish/ai-customer-assistant`
**Branch:** `features/ingestion` (last commit `d822ce5`; the value-normalization and entity-fragmentation work is uncommitted at the time of writing)
**Report date:** 2026-09-05
**Last updated:** 2026-09-11 — every P0/P1/P2 item, **P1-7** (superseded content), and **every item in `ingestion.md`**; see the changelog
**Method:** full read of `backend/src` (21.6k LOC), `backend/tests` (14.2k LOC), `frontend/src` (4.8k LOC), migrations, Docker/Make tooling and docs; plus a live run of the test suite and of the Docker stack against the real database.

---

## 1. Executive summary

This is a **multi-agent RAG customer-support assistant** for Alpinist Studios, built on FastAPI + LangGraph + Postgres/pgvector, with a document-ingestion pipeline (crawler → MinIO → Tika → chunk/embed → LLM entity extraction → EAV knowledge graph) and a dependency-free vanilla-JS frontend.

**Maturity: a working end-to-end prototype, not yet a deployable product.**

| Dimension | State |
|---|---|
| Architecture & module design | **Strong.** Clean layering, dependency injection everywhere, pure functions separated from I/O, excellent docstrings. |
| Feature completeness (MVP scope) | **~80%.** Public visitor chat, staff chat, RAG, ingestion, crawling, graph browsing, ticket creation, ticket status lookup and the admin *read* surface all work. The admin *write* surface and prompt management do not. |
| Test suite | **1040 passing / 0 failing / 0 erroring / 13 skipped** (1053 collected; the 11 extra skips are the opt-in live classification golden set). **Fully green** — the Playwright browser is installed, so the last non-deterministic gap is closed. |
| Production readiness | **No known blockers, and no open correctness issues** — P1-7 closed 2026-09-10. Authentication, authorisation, a CORS allowlist, rate limiting and an SSRF guard on the crawler are all in place (P0-3). Conversation state no longer reaches the logs (P0-4). Secrets are no longer injected by import side effect (P0-2), and the client/server timeout ladder no longer inverts (P2-4). |
| Scalability | **Much improved.** All four P1 items are fixed: pgvector-native retrieval, LLM calls off the event loop, one shared connection pool, and ingestion moved out of the web process into a worker service. Two pieces of per-process state that quietly broke horizontal scaling now live in the database (P2-5). |
| Repo hygiene | **Good.** The duplicated ontology is gone (P2-1); dead files, the committed AI-assistant note, the crawl artefact and the committed debug values are all gone, and the README is real (P2-6). Only the stale branches are left, deliberately untouched. |

**Every P0, P1 and P2 is fixed** — see the changelog. **P1-7 closed 2026-09-10**: superseded structured facts used to accumulate with no way to tell which was current, so two contradictory prices could be returned as equally true. Facts now carry provenance and a supersession mark, retrieval returns only current values, and the old value is kept rather than deleted so a temporal feature remains buildable. Semantic search over chunks deliberately still sees only current versions. P0-3, the last blocker, closed on 2026-09-08: the API is authenticated and authorised, CORS is an env-driven allowlist, requests are rate limited per user, and the crawler refuses to fetch anything that is not a publicly-routable host.

What is left is *unbuilt features* rather than defects — the admin write API, prompt management, CI and lint configuration, and structured logging with metrics. Those are listed in §5 and §7.

### Changelog

**2026-09-17 — One bad answer bricked a conversation permanently.** Asked for an email address, a customer replied *"wait i donot want to book it"* — which is ordinary behaviour, not an exceptional condition. `ticket_agent` passed the raw message to `create_ticket`, `InvalidEmailError` escaped the node, and the damage went far past failing that turn: **every subsequent message on the thread, on any subject, returned the same error.** "hello" three turns later still raised `InvalidEmailError: 'wait i donot want to book it'`. On `POST /chat` it was **HTTP 500 with an empty body**; on `/chat/stream` it rendered as the generic apology, which is what made it look like the earlier quota problem and not a defect.

The mechanism is worth recording because it will catch the next node too. Inspecting the poisoned checkpoint:

```
next     = ()
tasks    = 1
  name='ticket_agent'
    interrupts = (Interrupt(value={'type': 'email-collection', ...}),)
    error      = InvalidEmailError("'nope changed my mind' is not a valid …")
```

**A failed task keeps its interrupt alongside its error.** `_prepare_turn` decided "is this message a resume?" from `snapshot.next or any(task.interrupts …)`, which cannot tell a graph *waiting for the customer* from one that *crashed mid-flow*. So each new message was delivered as a resume to the dead task, which replayed its stored input, failed identically, and stayed dead. Self-perpetuating, exactly like the `persist_chunks` duplicate-chunk state: once poisoned, nothing the customer could type would clear it.

**Two fixes, because either alone leaves a hole.**

*The node no longer raises on ordinary input.* Email collection is a bounded loop (`_MAX_EMAIL_ATTEMPTS = 3`): an unusable answer re-prompts with `_TICKET_EMAIL_RETRY`, an explicit withdrawal ends the flow with `_TICKET_CANCELLED` and books nothing, and three failures give up with `_TICKET_ABANDONED` rather than pausing the thread forever. `interrupt()` inside a loop is sound — LangGraph replays the node from the top and satisfies interrupts in call order, so answered ones return their stored values and only the newest pauses. Cancellation is also offered at the *reason* step, since that is the other place the customer is asked a direct question. `_looks_like_cancellation` is a pure predicate over an explicit marker list, deliberately conservative: a false positive silently abandons a ticket somebody wanted, while an unmatched message merely gets asked again. Anything containing `@` is excluded first, so `dont.want.spam@example.com` books the ticket it was supplied for instead of cancelling it.

*The serving layer no longer resumes a task that carries an error.* A healthy pause has an interrupt and **no** error, so the error is the discriminator. When one is present the pending flow is abandoned, the message starts a fresh turn — which is what the customer meant by sending it — and a warning names the thread and the error. This is the backstop for the next node that raises, not the fix for this one.

Verified live against the rebuilt image, on the exact transcript that failed: the withdrawal answers *"No problem — I haven't created a ticket"*, and the following *"what is alpinist studios?"* and *"hello"* both answer normally, all **HTTP 200**. A typo path too: `satish.gmail.com` → re-prompt → `satish@gmail.com` → ticket created → the thread keeps working. And a thread already poisoned **before** the fix recovers on its next message, logging `abandoning a failed pending task and starting a fresh turn`.

Suite 1016 → **1040 passing**.

**2026-09-17 — Chat latency: a turn cost more than the whole per-minute budget.** The complaint was "retrieval is slow". Retrieval was **0.2s** — `vector_search` 0.16–0.29s, `structured_lookup` 0.02s, the SQL itself 68.8ms on 78 chunks, and every pure node (rank, deduplicate, build_context, build_prompt) at 0.00s. The knowledge graph measured node by node, with no contention, runs end to end in **3.0s**. The 30–40s turns were something else entirely.

Measured by reading the provider's own headroom before and after a turn and correcting for refill: **one turn cost ~11,478 tokens against an 8,000-token minute.** Not two turns — *one*. Even starting from a full bucket a turn had to stop partway and wait, because Groq does not refuse an oversized request, it *holds* it until the bucket refills at ~133 tokens/second. That is why no 429 ever appeared in the log for the slow turns, and why "it's throttling" looked wrong.

The arithmetic predicts the latency exactly, which is what makes it the diagnosis rather than a theory: short by `11,478 − 8,000 = 3,478` tokens, `3,478 ÷ 133 = 26s` of waiting plus ~4s of compute — against 30.0s observed. An isolated 2,279-token call with 613 tokens of headroom predicted 12.5s and measured 13.73s.

Where the tokens were, captured from the real prompts at all four calls: **answer 27,038 chars (59%)**, **classify 10,801 (24%)**, extract 4,920 (11%), rewrite 2,732 (6%).

**Two changes, both measured before being made rather than after.**

**Retrieval context: `KNOWLEDGE_AGENT_TOP_K` 8 → 4.** `scripts/calibrate_retrieval.py` against the golden set says 8, 6 and 4 all hold **recall@k at 100% and MRR at 0.933** — identical — while mean chunks reaching the answer prompt fall **5.2 → 3.4** at the configured margin. **top_k=3 is where it breaks** (recall 96.15%, MRR 0.923), so 4 is a measured floor and not a guess. Config only.

**The classification prompt: 11,359 → 6,498 characters (−43%).** It is sent on every turn and nothing downstream can sanity-check its output — `parse_llm_response` degrades anything malformed to `OUT_OF_SCOPE`/`UNKNOWN` *silently*, so a prompt edit that made the model worse would surface as "the assistant started refusing things" with nothing in any log. Removed: seven of ten worked examples (every rule they illustrated is stated in prose above them; the three kept are the hard discriminations — a greeting carrying a real question, a bare "ticket", and a follow-up that only resolves against history), a duplicated output-format block (the schema was specified three times, and the transport already constrains it — `response_format={"type": "json_object"}` on Groq, a prefilled brace on Anthropic), and a step-by-step procedure that restated the rules immediately before stating them. **No decision rule, literal value, confidence band or priority ordering was removed.**

That claim is enforced rather than asserted. A new `test_classification_golden.py` pins the structure without a network — every literal `parse_llm_response` maps, every field name it reads, the `{DOMAIN_DEFINITION}` placeholder, the 0.8 clarification threshold that `decide_route` also uses, and the five hard discriminations — plus a character ceiling that the old prompt fails. On top of that sits an **11-case live golden set**, opt-in like the Groq integration test, chosen for discrimination rather than coverage. **11/11 before the trim, 11/11 after.** The live run itself dropped from 149s to 67s, which is the token effect showing up independently.

**Result, measured the same way as the diagnosis: turn cost 11,478 → 9,013 tokens (−21%), and one question 30.0s → 10.5s.** On the question that prompted this, 33s → **9s**.

**Third change, and the one that crossed the line: the token bucket is per model, not per account.** 9,013 was still above 8,000, and trimming the remaining ~1,000 tokens looked expensive — until measurement showed the constraint was the wrong shape. Spending 2,780 tokens on `openai/gpt-oss-20b` left `openai/gpt-oss-120b`'s headroom **untouched at 7,927**: each model has its own 8,000/minute allowance. So the fix was not to cut tokens at all but to stop putting all four calls in one bucket. `KNOWLEDGE_AGENT_REWRITE_MODEL_NAME=openai/gpt-oss-20b` moves rewrite and extraction (they share one model field, `providers.py:226-229`) off the bucket the answer call needs. Config only.

**Final, measured the same way throughout: the 120b bucket carries ~6,679 tokens a turn and the 20b bucket ~2,605 — both under 8,000, so the refill wait is gone.** One question **30.0s → 4.6-9.0s**. And the complaint that started this: *"what is alpinist studios?"* then *"what is mvp development?"*, which used to answer once and then apologise, now answers **both** — 6.5s and 27.4s.

**The quality cost is real but small, and it is recorded rather than assumed.** Extraction on 20b agrees with 120b on the retrieval strategy for **5 of 8** probe questions; on the other three it fails to name an entity type and the turn degrades from `hybrid` to `vector`, losing the structured-fact half. Checked end to end, those three still answered well and cited sources (1, 3 and 2 citations, 4.6-5.9s) — the semantic index carried them. The residual risk is a question whose answer lives *only* in the graph and not in any chunk's text; three probes cannot rule that out. The conservative alternative, if that ever bites: move the **classifier** to 20b instead and keep extraction on 120b — arithmetic says ~6,800 on the 120b bucket, also under the ceiling — which needs the classifier's model to become configurable (hardcoded at `llm_client.py:165`).

Two genuinely fast questions inside one minute remains out of reach: it needs ~8,000 tokens per turn across both buckets and a turn costs ~9,300. That is the free tier's floor, not something trimming reaches.

Suite 996 → **1016 passing**.

**2026-09-17 — Anthropic is now a first-class provider: the Supervisor could not classify with Claude.** The Knowledge Agent has had an `AnthropicKnowledgeProvider` since the provider split, and the Admin › API Keys page has always listed Anthropic — but the Supervisor's `_PROVIDER_FACTORIES` held only `stub`, `gemini` and `groq`. Pasting a Claude key therefore produced a system that **answered with Claude and classified with the stub**, which returns `OUT_OF_SCOPE` / `UNKNOWN` for every question and raises nothing. Verified before the fix: an Anthropic-only environment resolved to `AnthropicKnowledgeProvider` for the answer and `StubSupervisorLLMClient` for the route. The app would have started cleanly and declined every question, with no error anywhere — the same silent-failure shape as P1-6 and F9.

A new `AnthropicSupervisorLLMClient` mirrors the Groq one: the same wall-clock-bounded retry loop, the same `note_rate_limited()` on a throttle, the same `sleep_within_budget` guard. Two API differences are absorbed at this boundary rather than pushed onto callers. **There is no `response_format={"type": "json_object"}`** on the Messages API, so JSON comes from *prefilling* the assistant turn with an opening brace — which constrains the first token — and prepending the brace back; `parse_llm_response` already degrades a malformed payload to the safe fallback, so a model that ignores the prefill costs one classification rather than the turn. And **the system prompt is a request field, not a message**, as `AnthropicKnowledgeProvider` already sends it.

The subtle one is message normalisation. Anthropic requires the first message to be `user` and rejects two consecutive messages with the same role; Groq and Gemini tolerate both. Neither holds here, because `node._bounded_history` keeps a *tail* of the conversation and can cut a user/assistant pair in half — so a perfectly ordinary follow-up question would have been rejected by the API on a history that starts mid-pair. `_anthropic_messages` drops leading assistant turns and merges same-role runs, and it is a pure function with its own tests.

Second defect, found while fixing the first: **auto-selection could not reach a provider the hard-coded tuple did not name.** Both resolvers tried `(admin default, "groq")` — the Knowledge one also `"gemini"` — and then gave up to the stub. Since `default_provider()` falls back to `"groq"` when no administrator has chosen, a deployment holding only an Anthropic key never reached it even once the factory existed. Both now sweep every registered provider after their preferred order, which is what makes *"paste a key for any supported vendor and it works"* true.

**Nothing changes for the current deployment**, and that is asserted rather than assumed: with only `GROQ_API_KEY` set, resolution still gives `GroqSupervisorLLMClient` and `GroqKnowledgeProvider` on `openai/gpt-oss-120b`. Groq stays the default because it is what the timeout ladder was measured against. Verified live after rebuilding the image: a chat turn answered in 6s with a citation.

An OpenAI key can still be stored and still resolves to nothing — there is no OpenAI client on either side. That is now a known gap rather than an invisible one.

Suite 980 → **996 passing**.

**2026-09-14 — Chat became the front door: a `visitor` role, public chat, and the sign-in page as a side door.** The product had one way in — the login page — and that was backwards for what this system is. A stranger asking the assistant about the company is the *main* audience; uploading documents and browsing the knowledge graph is staff work. So the flow inverted: the app opens on the assistant, and **Staff sign in** is a button in the header rather than a wall.

Three roles now, not two: `visitor` < `member` < `admin` (`auth/roles.py`). The alternative considered was renaming `member` to `visitor` and keeping two, which is strictly worse — the role travels inside the JWT, so renaming it invalidates every live session and needs a data migration plus a rewrite of every call site. **Adding a tier below the existing one costs neither**, because `satisfies` is a rank comparison: inserting `visitor` at rank 5 made every existing `require_member` guard start excluding visitors **without a single guard being edited**. Ingestion, crawling and the graph API were correct the moment the rank existed. Ranks are spaced by five so a further tier fits between any two without renumbering. Migration `f3c72a1d8b64` widens `ck_app_user_role`; that constraint had lived only in a migration, so SQLite tests had no constraint at all — it is declared on the `AppUser` model now, where the tests can see it.

**The chat router is public** (`dependencies=[Depends(enforce_chat_quota)]` on the router itself, no auth dependency). Public chat spends money, so the quota is the boundary rather than the login: anonymous callers are keyed by client IP instead of user id, and a third limit, `CHAT_GLOBAL_PER_DAY` (400, tunable), caps the *whole deployment* — per-caller limits do not bound a bill when the callers are strangers. Provider rate-limiting is now surfaced rather than swallowed: `rate_limit_signal.py` carries the fact out of the model call so the turn can say so. It holds a **mutable dict** in the `ContextVar`, not a boolean, because `asyncio.to_thread` copies the context — a plain `.set()` inside the worker thread never propagates back to the caller, and the signal was silently always false.

A related non-bug worth recording: a chat failure reported from the UI turned out to be Groq's per-minute token ceiling, **measured at 6,871 tokens per turn against a 8,000/min budget**, not a defect in the turn.

**Visitor chat UI.** A trimmed chat for signed-out callers: suggested openers, friendly source labels instead of raw identifiers, no sidebar. Fixed while building it: `renderAssistant` interpolated model output straight into `innerHTML` in its heading and list branches — escaped text in one branch is not a policy, so `inlineMarkup()` is now applied to already-escaped text in **every** branch. The sign-in page gained a **Back to the assistant** button below Sign in; `type="button"` is load-bearing, since an unqualified `<button>` inside a `<form>` defaults to submit.

**Ingest page: what is already in the corpus.** The page could add sources but not show them. `GET /ingest/sources` (member-guarded) lists them split into files and URLs — split on an `manual_upload://` reference prefix rather than on `origin_system`, which reads `"crawler"` for uploads too. Inactive sources are excluded and chunk counts come from one grouped aggregate rather than a query per source. Live: **23 sources, 6 files and 17 URLs**.

**Graph explorer.** The complaint was that the graph was unreadable; the measurement said the cause was the data, not the renderer — 479 entities at **average degree 2.12 with 24% isolated**, which no layout can make legible. So the view gained topology it can act on: degree-scaled radii, folded low-degree neighbourhoods with a count badge, a legend ranked by frequency and capped at ten types, and filters that hide isolates. Two defects fixed by the same work: `createRadialGradient` was called with non-finite coordinates because force-graph paints nodes before the layout has placed them, and the visible-link computation was **O(n²)** — roughly 485,000 comparisons per render — now an index rebuilt only on change.

**Corpus review.** One source 404s on the live site and was removed and re-ingested from the real contact page; a second stale Contact source went with it; thin sources were swept. The review also corrected a concern raised three times in this document's history: staff names in the corpus come from the public About page and published testimonials, so treating them as PII was over-cautious. Still open and needing a decision from the owner: whether *"internal / public company information"* on the four chapter PDFs means publishable, whether to deactivate `senior-artificial-intelligence-ai-engineer` (now 404), and whether to author a careers document — the `career` page is hollow because trafilatura discards the job list, application steps and benefits as page furniture.

Suite **980 passing**, 2 skipped. Verified in a browser end to end: landing on the visitor chat, reaching sign-in from the header, and returning with the back button, with no console errors.

**2026-09-09 — Ingestion: pipeline resources built once per process, and the worker image rebuilt.** `_resolve_deps` constructed everything per call and the worker calls it per job, so the 400 MB embedding model and its tokenizer were loaded from disk **once per document** — `Loading weights: 199/199` appeared in the log for every job, while `pipeline.py`'s docstring claimed the opposite. A new `PipelineResources` holds the session-independent half and is built once by `get_pipeline_resources()`; `_resolve_deps` now only binds the session. `scripts/run_worker.py` builds it at startup, so a broken model or missing MinIO config fails at boot rather than becoming a mystery failure on the first document. **Verified live: three jobs claimed, one model load.** The `httpx.Client` moved there too — it was previously created per job and never closed, leaking a descriptor per document — and is closed in the worker's `finally`.

Also: `_default_extraction_agent` read `GROQ_API_KEY` from the environment directly, so a key saved on the Admin › API Keys page reached chat but not ingestion, silently. It resolves through `llm_credentials` now, with the same environment fallback.

**The worker image was rebuilt and the container restarted on it.** It had been running code from the day before — including the `persist_chunks` bug that failed 31 jobs — so any `docker compose up worker` would have reintroduced it. Worth knowing for next time: the `worker` service has no `build:` section of its own (it reuses `ai-customer-assistant:local`), so `docker compose build worker` reports *"No services to build"* and silently does nothing; **`docker compose build backend` is what rebuilds the image the worker runs.** Suite 827 → **836 passing**.

**2026-09-09 — Ingestion: extraction calls batched (`ingestion.md` §5 item 8).** Extraction was making one model call per *window*, and chunks over 1800 characters are split into several windows — so a 5-chunk document made 10 calls. Measured first: the system prompt carries the whole canonical vocabulary at **896 tokens** against ~450 tokens of actual content, so **two thirds of every call was the same text sent again**.

Windows from every chunk are now flattened, batched three to a call, and mapped back by `window_id` before merging per chunk. Verified live: `news` went from 10 calls to 4, and two other documents from 6 to 2 each. Batching across chunk boundaries rather than within them is deliberate — batching within a chunk leaves short documents sending batches of one.

`INGESTION_EXTRACTION_BATCH_WINDOWS=1` restores the old behaviour exactly. A window the model omits costs that window, not the document. Also fixed: `_MAX_COOLDOWN_WAIT` was 420s inside a 600s stage budget, so one rate-limit cooldown could eat 70% of the time available for a whole document — now 60s, which is what a per-minute token bucket actually needs. And a duplicate `extract_chunk` that had been shadowed since windowing was added was removed.

**Not resolved:** the three large documents still are not ingested. They now fail on the Groq **daily** token budget (200,000 TPD, spent by the day's repeated re-runs) rather than on per-minute throttling — a quota ceiling rather than a code problem. Their jobs are left `QUEUED` and will ingest on the next worker run once the daily budget rolls over. Suite 813 → **827 passing**.

**2026-09-09 — Ingestion: the extraction stage is bounded, and the backlog re-run.** With the `persist_chunks` fix in place and a fresh Groq key, the 15 documents that had never ingested were requeued (one job each — the 60 failed rows were repeat attempts at the same 15 files). **Nine ingested successfully: indexed documents 9 → 18, entities 339 → 410, relations 242 → 334, with zero duplicate chunk groups.** Every `persist_failed` and every 429 disappeared.

Three failed on `400 Failed to validate JSON` — the model emitting malformed tool-call JSON, which is not transient and would not be helped by retrying.

The re-run also exposed a new defect: one document held the worker in `RUNNING` for **28 minutes** with no output, stalling the whole queue, because the worker is serial and the Groq client had no timeout. The stale-job reaper does not cover this — it runs once at worker startup, so a worker that is alive and stuck blocks forever. Fixed: `INGEST_EXTRACTION_CALL_TIMEOUT_S` (30s, on the client, with `max_retries=3`) and `INGEST_EXTRACTION_STAGE_BUDGET_S` (600s, around the stage) now sit in `timeouts.py` in their own section — ingestion is background work and may be slower than a chat turn, but not unbounded. A new `eav_extraction_timeout` failure kind separates "never came back" from "the model refused this". Verified live: the same document produced a clean timeout and the queue drained instead of stopping.

Also fixed while testing it: `ingestion/queue/__init__.py` eagerly re-exported `run_worker`, creating a circular import that fired only when `ingestion.pipeline` was imported first. Nothing used the shortcut — `scripts/run_worker.py` imports the module directly — so it bought nothing and cost a load-order trap. Suite 807 → **813 passing**.

**Still open:** three large documents (`news`, `2`, `artificial-intelligence`) remain un-ingested. They now fail visibly rather than hanging, but extraction makes one serial LLM call per chunk and a free-tier per-minute token budget cannot finish them inside any sane wall clock — see `ingestion.md` §5 item 8 (batch or parallelise the extraction calls).

**2026-09-09 — Ingestion: chunk persistence made idempotent (P0 tier of `ingestion.md`).** 60 of the 82 ingestion jobs in the development database had failed; **31 of them to one bug**. `persist_chunks` appended rather than replaced, so re-ingesting a version wrote every chunk again, and `_link_entity_to_chunk`'s lookup by `(version_id, chunk_index)` then raised *"Multiple rows were found when exactly one was required"*. The state was self-perpetuating: once a version held duplicates, every later attempt failed identically and the document could never be ingested again without database surgery.

Four changes: `persist_chunks` deletes this version's chunks before inserting; a `uq_chunk_version_index` unique constraint makes the duplicate state unrepresentable; migration `b7d1e93a5c40` deduplicates the existing rows *before* adding the constraint (the other order fails against exactly the data it exists to prevent); and `_link_entity_to_chunk` no longer uses `scalar_one()` — the remaining case is a *missing* chunk, which means the model named an index the document does not have, and that is one bad extraction rather than a reason to fail the whole document.

**Deleting first loses nothing.** Chunks belong to a *version*, and a version is an immutable snapshot — `_stage_fetch_bytes` verifies the bytes still match the recorded checksum before any of this runs. Superseded versions have different `version_id`s and are untouched. (Whether they stay *searchable* is the separate open question, P1-7.)

On the live database the migration removed **58 redundant chunks across 31 duplicated indexes** — 125 rows down to 67 — leaving 22 versions with contiguous chunk indexes and every indexed source still searchable. Verified beyond tests: three consecutive re-ingests of one version leave four chunks, and a following two-chunk re-ingest leaves two, with no orphaned tail.

Two portability defects surfaced while writing the tests, both invisible on Postgres: `embedding`'s SQLite variant was `Text`, which cannot bind a list, and `chunk_id` had no Python-side uuid default. Both are why this code had no test before — the table could not be written to off Postgres, which is the gap the bug lived in. Suite 798 → **807 passing**.

**2026-09-09 — Provider API keys in the Admin UI.** A new admin-only **API Keys** page lists every supported provider (Groq, OpenAI, Gemini, Anthropic), shows which are configured, and lets an administrator paste or clear a key and choose the default. Groq is the seeded default.

This deliberately trades away some safety: a key that previously existed only in `backend/.env` can now live in the database. Three things make the trade defensible. Keys are **AES-GCM encrypted** under a key derived from `AUTH_SECRET` by HKDF, so a database dump alone is not a usable credential. The API is **write-only** — no endpoint ever returns a key, only its last four characters, which is enough to recognise one and not enough to use it. And the **environment still works**: resolution is saved-key first, environment second, so a deployment that never opens the page behaves exactly as it did before. The consequence worth stating: **rotating `AUTH_SECRET` makes stored provider keys undecryptable** and they must be re-entered. An unreadable row is logged and skipped at startup rather than raised, because one bad credential must not stop the app from starting with the others.

The page's `source` column ("saved" vs "from environment") exists because a saved key shadows the environment variable — without showing which is in effect, *"I changed the key and nothing happened"* is unanswerable.

Two defects found by measurement rather than reasoning. **(1) A read-after-write race:** `get_session` commits during dependency teardown, *after* the response reaches the client, so the page's immediate reload could miss its own write — observed as a save that appeared not to take. The mutating handlers now commit before returning. **(2) `server_default="false"` is a string literal**, which Postgres reads as the boolean but SQLite stores as the text `'false'` — and `'false'` is truthy, so a freshly inserted row came back claiming to be the default provider. Fixed with a Python-side `default=False` alongside it. Harmless on Postgres, wrong everywhere else; a test caught it.

Migration `9a4f7c2b83d1`, with a partial unique index enforcing at most one default in the database rather than in whoever remembers to clear the old one. Suite 774 → **798 passing**.

**2026-09-10 — One thing stored as several entities.** `"offers flexibility"`
appeared twice under `Agile / flexibility`, beneath a unique constraint that
should have made it impossible. The constraint was not violated: it is on
`(entity_id, attribute_id, value)`, and there were **three Agiles** —
`Methodology`, `Process` and `Development Process`, holding 39, 14 and 2 facts.
55 facts about one concept, across three identities the database considered
unrelated. 62 of 538 names were fragmented this way.

Three reasonable decisions that contradict each other: identity is
`(entity_type, name)`; the ontology passes unknown types through unchanged; and
the extraction prompt explicitly invites the model to invent a type when none
fits. The prompt asks for free-form types to protect recall while identity is
keyed on them. Underneath it all, `entity_type` is an attribute, not an
identity discriminator — "Python is a Programming Language" and "Python is a
Technology" are both true and neither makes it a different Python.

The cost was worse than the visible duplicates. `structured_lookup` matches on
type and name, falling back to name alone — so a query naming one variant
returned 39 facts and silently missed 16. A partial answer that looks complete
is more dangerous than an obviously duplicated one.

All 62 groups were reviewed before any code was written, and **not one was a
genuine homonym**. Entities now resolve by normalized name; `reconcile` merges
what was already there, deciding the surviving row (most facts), the label
(best casing, so a merge cannot rename `PyTorch` to `pytorch`) and the type
(rarest in the corpus as a specificity proxy, restricted to types carrying a
real share of the facts) as three independent choices.

Two bugs in the repointing, one found only by running it: colliding values were
matched on exact text after uniqueness had moved to `value_norm`, and the
survivor was relabelled before its duplicates were deleted — which fails when a
duplicate still holds the chosen `(type, name)`. The live run stopped on
`duplicate key value violates unique constraint "uq_entity_type_name"`. No
pure-planner test could have caught it, so the repointing half now has tests
against real rows, verified by reverting the fix and confirming they fail.

Live: 538 → 469 entities, 62 → 0 fragmented names, Agile's 55 facts on one row
(54 after the duplicate collapsed), values 832 → 822, provenance and relations
preserved. The two documents that previously produced two `Low-fidelity
Prototype` entities in a single run were re-ingested and produced zero new
fragmentation. Suite 951 → **969 passing**.

**2026-09-10 — One fact stored several times.** The corpus held five
`MVP / definition` values where the document states at most two, and
`Parbati B. / role` as both "PHP Intern" and "php intern". Reading the data
before writing any code turned one problem into three, each needing a
different fix.

**Text that differs invisibly.** `pre-defined projects` appeared twice,
differing by a single character — U+002D against U+2011 NON-BREAKING HYPHEN.
Not paraphrase at all. `value_norm` now holds a case-folded,
whitespace-collapsed, ASCII-punctuation form and `(entity, attribute,
value_norm)` is unique, making the duplicate unrepresentable rather than
relying on every writer. The migration repoints provenance before deleting a
duplicate: the FK cascades, so deleting one would drop the record that a
version asserted the fact, and the survivor would then be marked superseded —
a merge deleting an answer through a side effect two tables away.

**The model paraphrasing across overlapping windows.** Folded within a
document, keeping the longer telling. The threshold was measured rather than
picked: every duplicate pair in the corpus was scored, and real duplicates sit
interleaved with real distinctions between 0.70 and 0.91 — two URLs at 0.893,
two prices at 0.706, two different interests at 0.703. The line goes above
that band at 0.92 and deliberately misses one true duplicate, because leaving
a redundant row costs noise while merging two prices deletes an answer.

**Windows cut mid-word.** The corpus contains "smallest yet fun" — "smallest
yet fun|ctional version of the product" with a boundary through the middle of
"functional", extracted as a complete fact. No similarity rule repairs it: the
strings score 0.35 against each other, and the missing half was never sent to
the model. Windows now end on a sentence boundary or whitespace.

Throughout, the constraint was not to break genuinely multi-valued attributes:
`Agile / stage` keeps six real stages and `Alpinist Studios / objective` six
real objectives, both pinned by tests.

**What this did not fix, and what the evidence now points at.** The dominant
remaining source of apparent duplication is **entity fragmentation**, not
value paraphrasing. Of 538 entities, 44 names are split across two or three
`entity_type` values and 19 differ only by case — `Agile` exists as
`Methodology`, `Process` and `Development Process`, and one re-ingest produced
`Prototype / Low-fidelity prototype` and `Product / Low-fidelity Prototype` in
the same run. Each variant collects its own facts and `structured_lookup`
unions them, so one fact reads as several. `reconcile.py` merges only rows
sharing a canonical type, so it does not touch these. That is entity
resolution, and it needs a product judgement: merging `Agile` the Methodology
with `Agile` the Process is right; merging `Architecture Review` the
`Practice` with the `Service` may not be.

Migration `e7b04d2c9a13` merged 2 rows live (777 → 775) with provenance
intact. Suite 921 → **951 passing**.

**2026-09-10 — Trace ids and concurrent lanes (P3-14, P2-12).** Two items
that turned out to be one: concurrency makes the log unreadable, and trace ids
are what make it readable again.

Every ingestion log line now carries `[<job8>.<attempt>]`. The id names the
*run* rather than the job, because a job can now be retried four times and
`job_id` alone would label all four identically. It travels in a ContextVar
rather than a parameter — the lines that need labelling come from modules with
no reason to know about jobs, and ContextVars propagate through
`asyncio.to_thread`, which is where extraction runs. The filter is attached to
the handler rather than a logger, since propagation is how nearly every line
here is emitted.

The worker now runs `PGQUEUE_CONCURRENCY` lanes (default 2) in **one process**,
sharing one copy of the embedding model. That is what made it affordable: a
second process loads its own 400 MB copy, which was the stated blocker. What
it buys is worth being honest about — extraction is bound by the provider's
token budget, not by this worker, so lanes do not double throughput against a
daily ceiling; they stop one slow document holding the queue head.

Two hazards. The shared `SentenceTransformer` is not safe to encode from
several threads at once and `chunk_and_embed` runs under `to_thread`, so
encoding is now serialised with a lock — cheap, because embedding is a small
fraction of a document's time. And a race the plan had assumed away: `FOR
UPDATE SKIP LOCKED` stops two lanes taking the same *row*, but the
one-job-per-source guard is a different question, and a lane's RUNNING
transition is invisible to the others until it commits — so two lanes could
each take a different job for the same source. Claims are now serialised for
the duration of the claim.

Verified live: two documents claimed 187 ms apart and extracting
simultaneously, three jobs all `SUCCEEDED`, corpus unchanged. Both documents
were named `Contact`, which is exactly why the trace ids had to come first.
Suite 911 → **921 passing**. **Every item in `ingestion.md` is now closed.**

**2026-09-10 — Superseded facts (P1-5), the last open ingestion item.** The
question that raised it: *"if the rate to build a website changed, can the
assistant still tell me what it used to be — and does it know which one is
current?"* No on both counts. `value` rows are unique on (entity, attribute,
value), so $500 and $800 both survived under the same entity and attribute
with no version link, and structured lookup returned both. Two contradictory
prices presented as equally true is a *wrong* answer delivered confidently,
which is worse than a missing one.

**Decision: keep history and mark it, rather than prune it.** The old rate is
a real fact, and deleting it makes the original question permanently
unanswerable. Retrieval filters to current values, so the contradiction stops;
the history accumulates for a temporal feature that can be built later without
another migration. The chunk half is deliberately unchanged — semantic search
still sees only current versions, so no answer can cite replaced content.

The live data was not what the item predicted. 48 entity+attribute pairs held
several values, but there were **zero** `STALE` versions — so none were
superseded content. They were genuinely multi-valued attributes (four client
industries, all true), extraction noise (five paraphrases of one sentence),
and no changed prices at all. `Attribute.multivalue`, the column that exists
to tell the first from the third, was hardcoded `False`.

Provenance is a link table rather than a column, because a `value` row is
global — the same fact is often stated by several documents — and without that
supersession is not decidable. Currency is *derived* from provenance on every
run rather than accumulated, so a re-ingest, a rollback, or a document ceasing
to be current all converge on the same answer without anyone reasoning about
order. Facts that come back are un-marked; the pass runs after cutover, since
`current_version_id` is what "current" means; a failed pass does not fail the
job; and values with no provenance are never touched, which is why the
migration needs no backfill.

`multivalue` is now observed rather than asked for, counted across the whole
document and per entity — verified live: 15 people with one role each, still
`multivalue=false`. Both value queries also gained an `ORDER BY`; neither had
one.

Verified on live Postgres. A throwaway source with two versions: `$500` stood
under v1 and `$800` was superseded; cutting over to v2 swapped them; a re-run
changed nothing; rolling back to v1 swapped them again. Then a real document
was re-ingested — 15 provenance rows, 0 values wrongly superseded, corpus
unchanged at 777 values across 24 indexed sources. Migration `d5a91c3f7b28`.
Suite 897 → **911 passing**. **Every P0 and P1 item in `ingestion.md` is now
closed.**

**2026-09-10 — The worker loop gets tests, and they find a day-old bug.**
`poll_once` is the seam where a claimed job, a handler and the retry policy
meet, and it had no test at all. The files around it covered the reaper and
the pipeline's rollback but not the thing that calls them — and after the
retry work `poll_once` can write four different statuses, which is the
difference between a document that recovers by itself and one that stops
forever.

The tests immediately caught a defect introduced the previous day. The retry
policy gave every transient failure a ten-minute backoff, and a job abandoned
by a dead worker is classified transient — so an ordinary deploy silently cost
*every in-flight document* ten idle minutes before it resumed. Both halves
were individually correct; only their composition was wrong, which is why no
unit test of `retry.decide` would have shown it.

The fix is a distinction the policy was missing. A backoff answers "the
condition that caused this needs time to clear" — a token bucket refilling, a
Tika restarting. A worker that died has already cleared by definition: the
process doing the reaping is its replacement. `retry.IMMEDIATE_KINDS` names
the kinds that requeue with no delay; the attempt cap still applies, which is
what actually protects against a document that kills every worker that touches
it.

Also newly covered: the one-job-per-source guard, which had never been tested
despite being what makes `FOR UPDATE SKIP LOCKED` safe to point at more than
one worker. Suite 875 → **897 passing**.

**2026-09-10 — Ingestion retry, dead-letter, and structured failures.**
Every ingestion failure was terminal. `complete_job` wrote `FAILED` and the
document stopped there, whatever the reason — so a job that hit a per-minute
rate limit, or a Tika that happened to be restarting, needed a human to notice
and re-queue it by hand. Once the deterministic failures were gone, that was
the *only* kind of failure left in this database: the last two documents died
on a daily token ceiling that a retry an hour later simply walks past.

`ingestion/queue/retry.py` decides on `(failure_kind, attempt_count)` and
nothing else — no I/O, no clock beyond `now` — so the interesting question is
answerable in a unit test without a database or a provider. A transient
failure is requeued with a backoff; one that exhausts its four attempts
becomes `DEAD_LETTER`; a terminal failure stays `FAILED` after one attempt.
`FAILED` and `DEAD_LETTER` both need a human but need different things from
one, which is why collapsing them was the gap.

Failures are classified **terminal by default** — only five kinds are
transient — so a failure nobody has classified yet stops after one attempt and
stays visible rather than quietly spending a retry budget. Rate limiting got
its own `Err` code, split from `eav_extraction_failed` where the exception is
still in hand rather than by re-parsing a message two layers away. The
stale-job reaper now counts its requeue as an attempt, so a document that
kills the worker every time dead-letters instead of being requeued forever by
the very mechanism meant to rescue it.

`failure_kind` became a real column (the long-standing item 13) because the
policy has to dispatch on it, and doing that with `split_part(error_details,
':', 1)` is exactly the fragility that item was about. It was backfilled, so
the 49 failures already in the database are groupable immediately. The Jobs
page gained **Attempts** and **Failure** columns; status alone cannot say
whether a `QUEUED` row is fresh work or a job waiting out its backoff.

Verified against live Postgres, not only SQLite — the enum value and the claim
SQL are what SQLite cannot prove. A temporary job row was held back by its
backoff, became claimable when it elapsed, was requeued on a simulated 429
with `completed_at` left null, reached `DEAD_LETTER` when its budget ran out,
and a `checksum_mismatch` went straight to `FAILED` after one attempt; the row
was then removed. Migration `c4f8b2e17a90`. Suite 853 → **875 passing**.

**2026-09-09 — Ingestion: the last two unindexed documents.** With the P0
persistence work and the quota problems behind it, 22 of 24 sources were
indexed. The two that were not — `sdlc.pdf` and `tech_stck.pdf`, both at zero
chunks — failed on one thing: `400 Failed to validate JSON`, a class
`ingestion.md` had noted and then set aside without giving it a work item.

Reading the code found two defects behind it. `_is_retryable` matched the
substring `"json"`, and Groq's rejection message contains the word — so every
rejection was retried five times at `temperature=0` with an identical prompt,
five identical failures, five times the tokens against the daily budget that
was the binding constraint. And because batching put three windows in one
call, a rejected call lost all three and failed the document; the empty
`failed_generation` says the response outgrew what the model emits for three
windows at once, which is a request-size problem, not a content one.

A rejected batch is now split in half and retried recursively — repeating a
deterministic request is never the answer to it, but a smaller request can
be. Only deterministic rejections split (splitting a throttled batch makes
two throttled calls), a document whose every window is rejected still fails
rather than succeeding with an empty graph, and a lost window is logged with
its chunk index. Recorded as item 8b in `ingestion.md`.

Verified on the real rejection: `sdlc.pdf` logged `batch of 3 window(s) ...
was rejected; splitting into 1 and 2`, then lost the one window that still
failed at width 1 and kept the other two — `Extracted sdlc.pdf with 1 of 9
window(s) lost`, where the same rejection previously ended the document. That
the isolated window failed alone says both causes were real: one window here
genuinely cannot be extracted, and the other two were collateral damage from
sharing its call.

**All 24 sources are now indexed for the first time** — 77 chunks (from 67),
491 entities (410), 768 values (708), 374 relations. Suite 846 →
**853 passing**.

**2026-09-09 — Ticket status lookup.** The last hardcoded promise in the core product. `CHECK_TICKET_STATUS` was classified correctly, reached `routing.py`, and was answered with a fixed string — *"Ticket status lookups aren't available yet"* — regardless of what the customer asked, while the `ticket` table held 7 real rows. A customer who had just been handed a ticket id in a confirmation could not ask what had become of it.

Two pieces. `TicketStore.get_ticket(ticket_id)` is the read half of a store that until now only wrote. A new `ticket_status` graph node sits beside the ticket agent rather than inside it: the ticket agent's entire job is *creating* a ticket, and it does so by interrupting twice to collect a reason and an email — routing a status question there would have opened a second ticket instead of answering about the first. `routing.py` now names the new destination, and `NextAgent` gained `TICKET_STATUS_AGENT`.

The node answers in one turn when the message already carries an id (`"any update on 974de0b9-…?"`), and `interrupt()`s once for the id when it does not — the same pause/resume mechanism the ticket-creation flow uses, so the serving layer needed no change.

**Tickets are identified by id, not by email.** An email lookup would be friendlier — nobody keeps a uuid to hand — and it would let anyone who can name an address read that person's tickets, in a chat surface where the address is typed rather than proven. The id is what the confirmation gave them and it is unguessable. Doing this by email needs the ticket bound to the authenticated `AppUser`, which is worth building and is not a lookup change.

Three things the implementation is deliberate about. A **malformed id is a miss, not an error** — the id arrives from a person typing into a chat box, and "no ticket with that id" is the honest answer to `abc123` as much as to a well-formed uuid that does not exist; raising would turn a typo into a failed turn. A **miss is reported as a miss**, never as a status, because inventing "that ticket is open" for an id that matches nothing is worse than saying it was not found. And the extracted id is **normalized to lowercase**, caught by a test pasting an id in the uppercase form a mail client renders: the fake store's lookup missed, and the real one would have echoed an id back in a different case than the confirmation showed.

Verified against the live database, not only the fakes: the node read ticket `974de0b9-…` back through the real `TicketStore` and rendered *"is currently **open**"* with the customer's own reason and address, and answered a bogus uuid with the not-found text. Suite 836 → **846 passing**.

**2026-09-09 — Admin API (read).** All four tabs of the Admin page had shown *"Endpoint not available yet"* since the page was written, while the data sat in the database: 24 knowledge sources, 82 ingestion jobs, 339 entities, 7 tickets. `api/admin.py` adds `GET /admin/knowledge-sources | jobs | stats | tickets`, admin-only, each returning `{ <list>, total, limit, offset }` rather than a bare array so a caller can tell "all of it" from "the first page of it".

The standing note in `main.py` and `frontend_plan.md` §6.2 pointed at `ingestion/storage/api.py` as the thing to wire. That was the wrong target and stayed wrong for months: it is a *write* path — a second `POST /admin/knowledge-sources` upload duplicating `POST /ingest/upload` — whose placeholder dependencies still raise `NotImplementedError`. Wiring it would have added a duplicate upload route and left every read tab empty.

Three defects surfaced only once the endpoints returned real rows. **(1)** The obvious job ordering, `started_at DESC NULLS FIRST`, filled the entire first page with the oldest failures in the database, because 37 of 82 rows predate the code that stamps `started_at`; it now sorts queued work first, then by whichever of `completed_at`/`started_at` a row actually has. **(2)** `.stats-grid .card` also matched `.card.stat-card`, so all four figures stacked full-width, one per row — invisible until the tab had data to lay out. **(3)** The Jobs table never showed *which source* a job was for, which is the first thing anyone asks of a list of failures.

Two model columns gained `with_variant(..., "sqlite")` — `knowledge_source_version.metadata` (JSONB) and `embedding_chunk.embedding` (pgvector). Postgres is untouched; the variants exist so these tables can be *created* off Postgres, which is what lets the new endpoints be tested against a real query rather than a mock. Suite 759 → **774 passing**.

**2026-09-08 — P0-3, the API is authenticated.** The last blocker. All 13 endpoints were anonymous, `CORS` allowed every origin, and `POST /ingest/crawl` would fetch any URL a caller named — `http://169.254.169.254/latest/meta-data/` included, which is cloud credentials in one unauthenticated request. Now: JWT (HS256) with a 15-minute access token and a 14-day **rotating** refresh token whose id is stored so it can actually be revoked; `HttpOnly; Secure; SameSite=Strict` cookies for the browser and `Authorization: Bearer` for scripts through one verifier; Argon2id passwords; two roles (`member`/`admin`). `/chat` is authenticated — an internal tool whose every turn spends Groq tokens against a shared daily budget. The public surface is exactly three routes, and `tests/api/test_route_protection.py` enumerates the assembled app to assert every other route refuses an anonymous caller.

Three things the implementation taught that the design had not: **(1)** the SSRF guard has to sit at the crawler's *fetch boundary*, not only at the API — a site is crawled by following its links, so a link to an intranet host went through the same code path; and `follow_redirects=True` was a bypass, since a public URL that 302s to the metadata endpoint sailed past a check on the URL the caller supplied. **(2)** Two security-relevant writes were being silently discarded: revoking every session on detecting a replayed refresh token, and the login rate-limit counter, both happen on the way to an error response, and `get_session` rolls back when a handler raises — so theft detection revoked nothing and the fifth wrong password was as unthrottled as the first. Both were found by tests asserting the *consequence* rather than the status code. **(3)** The frontend must coalesce refreshes: tokens rotate, so four parallel 401s would have replayed one refresh token four times, and the server correctly reads that as theft — an ordinary page load would have logged the user out and recorded it as an attack.

Also caught by measurement: on Python 3.12 `100.64.0.0/10` (carrier-grade NAT) reports `is_private=False` *and* `is_global=False`, so an enumeration of the named address flags let it through; the guard now refuses anything not globally routable. And the first version of the exhaustive route test found one route and passed — FastAPI 0.141 does not flatten included routers — which is why it carries a companion assertion that the enumeration is not empty.

Verified live against the running container: anonymous callers get 401 on chat, graph and ingest while `/health` stays open; cookies come back `HttpOnly; Secure; SameSite=strict`; no `Access-Control-*` header is emitted at all; six hostile crawl URLs are refused with 400 *while authenticated as an admin*; a replayed refresh token revokes the whole family; the sixth login in fifteen minutes returns 429 with `Retry-After`, and the counter includes the failed attempts. An upload now records the signing-in member in `knowledge_source.uploaded_by`; historical rows still point at the service account, which cannot be invented retrospectively. Migration `8e5a3c9d21f7`, additive. Suite 638 → **759 passing**.

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

**Current, after every P0/P1/P2 item, P1-7, the whole of `ingestion.md`, and the visitor access model:**
```
cd backend && env -u PYTHONPATH ./.venv/bin/python -m pytest -q
→ 1040 passed, 13 skipped in 88.70s
```

Collected: 1053 tests, **no failures and no errors**. The most recent 210
arrived with the ingestion work: retry policy and dead-lettering, the worker
loop, fact supersession, value duplication, entity merging. The last 121 arrived with
P0-3: tokens, passwords, roles, the auth router, rate limiting, the SSRF
guard, and `tests/api/test_route_protection.py`, which enumerates the
assembled application and asserts every route is either on a four-entry
public allowlist or refuses an anonymous caller. The runtime also dropped from ~131s to ~58s: the two opt-in tests that were making real network calls on every run now skip correctly. The 1 failure and 4 errors that had persisted through every earlier report were all the same missing dependency — a Playwright browser, installed with `uv run playwright install chromium`. Nothing in the suite is non-deterministic.

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
 Browser (vanilla JS, hash router, route-guarded by session.js)
   │  Cookie: access_token (HttpOnly, 15 min) — refreshed and replayed on 401
   │  POST /chat/stream (SSE)   POST /chat   GET /graph/*   POST /ingest/*
   ▼
 FastAPI  (main.py — also serves the frontend as StaticFiles at "/")
   │
   ├── auth/  require_role(member|admin) on every router below
   │     │    verify signature → load the row → check is_active → read role
   │     │    rate limit per user id, counted in the database
   │     └── /auth/login | refresh (rotating) | logout | me | users
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
   ├── /graph/*   read-only EAV graph browser  (member)
   └── /ingest/*  upload | crawl | discover | confirm  → 202 Accepted
                    │     every outbound URL passes auth/ssrf.py, at the
                    │     crawler's fetch boundary — so discovery, sitemaps,
                    │     downloads and followed links are all covered
                    └─► register_document_version → MinIO + job row
                          uploaded_by = the signed-in user
                                                          │
 worker service (scripts/run_worker.py) ◄─────────────────┘ claims the job
   └─► run_ingestion: fetch → Tika → chunk+embed
        → EAV extraction (Groq) → persist → cutover
```

Every Supervisor node is wrapped by `node_logging.log_node`: off at the
default `INFO`, and free text redacted to a shape summary even at `DEBUG`
unless `LOG_PII=true` (P0-4).

**Public surface: three routes.** `GET /health`, `POST /auth/login` and
`POST /auth/refresh`. Everything else requires a signed-in `member`, and
`tests/api/test_route_protection.py` enumerates the assembled app to prove
it — the list is a literal in the test, so widening it is a visible diff.

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
- **EAV extraction** (`ingestion/extraction/`): a **JSON-mode** agent over the shared canonical ontology, emitting entities / attribute-values / relations. One call covers several ~1800-character windows, batched across chunk boundaries; a rejected batch is split rather than failing the document. (`extraction/tools.py` implements the older tool-calling path and is now referenced only by its own test — see §5.)
- **Persistence** (`ingestion/persistence.py`): entity resolve-or-create **by normalized name**, so a type the model invents cannot mint a second identity; chunks are replaced rather than appended; values are unique on their normalized form, carry provenance per document version, and are marked superseded when no current document still asserts them.
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

**Login and route guard (P0-3).** The shell is otherwise unchanged from the original design: header, sidebar, cards. What authentication added is a login page, `session.js` holding the signed-in user, the signed-in address and role in the header, and a route guard in `router.js` that gates on two axes -- signed in or not, and the minimum `role` on the page.

**The front door is the assistant, not the login page (2026-09-14).** A signed-out caller lands on a trimmed visitor chat; **Staff sign in** is a header button, and the sign-in page carries a **Back to the assistant** button so reaching it by accident is not a dead end. Everything else still requires an account.

A studio three-column layout (icon rail / workspace / context inspector) with a warm-ink and then an aurora palette was built on 2026-09-08 and **reverted on 2026-09-09** at the user's request. The authentication work was kept; only the design was rolled back. The reverted design is recoverable from that day's history if it is ever wanted again.

| Page | Role | State |
|---|---|---|
| Chat (visitor) | — | Done — the landing page signed out: suggested openers, friendly source labels, no sidebar |
| Login | — | Done — reached from the header, not forced; `router.js` guards the rest and remembers where the user was going, and a **Back to the assistant** button returns to the visitor chat |
| Chat | member | Done — thread list in `localStorage`, retry, citations |
| Graph | member | Done — 2D/3D force-graph explorer: degree-scaled radii, folded low-degree neighbourhoods, ranked legend, isolate filter |
| Ingest | member | Done — upload, crawl, discover→review→confirm, job polling, and the list of sources already ingested split into files and URLs |
| Overview | member | Done |
| Prompt | **admin** | Partial — prompts are viewable/editable but **device-local only**; no backend write endpoint. Admin-gated because editing the agent's prompts changes how the system answers everyone, and it would be odd for that to become an admin action only on the day it starts persisting |
| Admin | **admin** | **Stub** — renders "not available"; the `/admin/*` endpoints it calls don't exist. When they are built they go behind the `admin` role, which is why that role exists now |

`router.js` gates on two axes: signed in or not, and the minimum `role` on
the page. Admin pages are hidden from the sidebar for a member and refused if
reached by URL, with a message rather than a silent bounce — being redirected
with no explanation reads as the app being broken. This is a usability layer,
not a security boundary: the API refuses unauthorised requests by itself, and
a determined caller skips the browser entirely. Verified by driving Chromium
as each role: an admin sees six sidebar entries, a member four, and a member
typing `#/admin` lands on Chat with *"That page is for administrators."*

`api.js` sends `credentials: 'include'` and, on a 401, refreshes once and
replays — through a **single shared** in-flight refresh. That coalescing is a
correctness requirement, not an optimisation: refresh tokens rotate, so four
parallel 401s would present one token four times, which the server correctly
reads as theft and answers by revoking every session the user has.

### 4.7 Tooling
`docker-compose.yml` (postgres/pgvector, MinIO + bucket init, Tika, backend), a multi-stage `Dockerfile`, an entrypoint that waits for Postgres and runs `alembic upgrade head`, a `Makefile` with `up/down/worker/ingest/verify/psql/trunc/chat/graph`, and a `langgraph.json` for LangGraph Studio.

`scripts/create_user.py` creates the first admin — there is no self-signup,
so every account after that comes from `POST /auth/users`. It prompts for the
password rather than taking it as an argument, because an argument lands in
shell history and in `ps` output for every other user on the machine.

---

## 5. What is NOT done

| Gap | Evidence |
|---|---|
| ~~**Authentication / authorisation**~~ | **Done (P0-3, 2026-09-08).** `auth/` holds tokens, passwords, roles, dependencies, the router, rate limiting and the SSRF guard. What is *not* built is anything behind the `admin` role beyond account creation — prompt management and source deletion are still unwritten endpoints, and that is why the role exists now rather than later. |
| ~~**Admin API (read)**~~ | **Done 2026-09-09.** `api/admin.py` serves `GET /admin/knowledge-sources`, `/admin/jobs`, `/admin/stats`, `/admin/tickets`, admin-only. All four Admin tabs now render real data instead of "Endpoint not available yet", and the Overview figures come from `/admin/stats` rather than a capped `/graph/search`. |
| **Admin API (write)** | Still unbuilt: no source deletion, no re-index trigger, no ticket status change. `ingestion/storage/api.py` remains unregistered — it is an *upload* path duplicating `POST /ingest/upload`, not the read surface the page needed. |
| ~~**Ticket status lookup**~~ | **Done 2026-09-09.** `TicketStore.get_ticket` reads the `ticket` table; a `ticket_status` node answers the `CHECK_TICKET_STATUS` intent that was previously routed away with a hardcoded "not available yet" string. What is *not* built: looking a ticket up by anything other than its id (see the note in §3), and changing a ticket's status, which belongs to the admin write surface. |
| **Prompt management API** | Frontend edits never reach the server. |
| ~~**LLM provider keys**~~ | **Done 2026-09-09.** `GET/PUT/DELETE /admin/llm-providers`, admin-only, keys encrypted at rest. What is *not* built: validating a key against the provider before saving it, and per-agent provider overrides — the default applies to every agent. |
| **Typed settings** | `config.py` now loads the environment (P0-2), but modules still read `os.environ` directly rather than a typed settings object. `agents/knowledge/config.py` shows the pattern to follow. |
| ~~**CHANGELOG**~~ | **Done** — `CHANGELOG.md` was written in the F-series and covers P0-3. |
| **CI** | No `.github/`, no lint config, no formatter config, no coverage gate. |
| ~~**Worker in Docker**~~ | **Done (P1-5)** — a `worker` service now runs `scripts/run_worker.py`; the API only enqueues. |
| **Observability** | No structured (JSON) logging, no metrics, no tracing. Log lines now carry a `[trace_id]` column and **ingestion fills it** — each job run is labelled `<job8>.<attempt>`, which is what makes concurrent lanes and retried jobs legible. The **chat path still does not**: it threads a `trace_id` through the LangGraph config but never sets the logging context, so its lines print `-`. |
| ~~**Streaming responses**~~ | **Done (F7)** — `POST /chat/stream` reports progress from ~50ms; `POST /chat` is unchanged as the fallback. |
| ~~**Rate limiting / abuse control**~~ | **Done (P0-3)** — per authenticated user, counted in the database rather than a process dict, so it survives a restart and does not multiply by the instance count. 5 logins / 15 min (per address *and* per account), 20 chat turns / min and 500 / day, 10 ingest calls / min. |
| **Session management UI** | `refresh_token` records a user agent and issue time per session, but nothing surfaces them. "Sign out everywhere" exists as a code path (it fires on detected token reuse) with no button attached. |

---

## 6. Current problems, ranked

### ✅ P1-7 — Superseded content: chunks hide history, facts cannot date it — **FIXED 2026-09-10**

**Found 2026-09-09**, while reviewing the ingestion pipeline. Not caused by any recent change; it has been true since retrieval was written. Surfaced by the question *"if the rate to build a website changed, can the assistant still tell me what it used to be?"*

The two retrieval paths answer that in opposite — and both wrong — ways.

**Semantic search keeps history and then hides it.** `cutover` marks the previous version `STALE` and deletes nothing, so every superseded chunk is still in `embedding_chunk`. But `agents/knowledge/vector_search.py:347` filters:

```sql
WHERE knowledge_source_version.version_id = knowledge_source.current_version_id
```

so a superseded chunk can never be retrieved. *"What was the previous rate?"* is unanswerable, and the data needed to answer it is sitting in the table.

**Structured lookup keeps history and cannot distinguish it.** `value` rows are written `ON CONFLICT DO NOTHING` on `(entity_id, attribute_id, value)`. When a price changes from $500 to $800, **both rows survive**, under the same entity and attribute, with **no timestamp and no version reference**. `structured_lookup` returns both and nothing marks which is current.

**The second is the more serious.** A missing answer is visibly missing. Two contradictory prices returned as equally true is a *wrong* answer delivered with confidence — and the F-series showed that structured facts lead the answer prompt, so the model is being handed the contradiction first.

**It was not yet visible when found.** Every chunk belonged to a current version; nothing had been re-ingested with changed content. It would have become real the first time a document was updated — which is exactly when nobody would be looking for it.

**The decision taken: keep history and mark it.** The old rate is a real fact, and deleting it makes the question that raised this permanently unanswerable. So:

* `value_provenance` records which document version asserted each fact — a link table rather than a column, because a `value` row is global and a fact one document drops may still be asserted by another.
* `value.superseded_at` marks the facts no current version still asserts. Currency is *derived* from provenance on every run rather than accumulated, so a re-ingest, a rollback, or a document ceasing to be current all converge without anyone reasoning about order.
* `structured_lookup` returns current values only, so the contradiction stops.
* **The chunk half is deliberately unchanged.** Semantic search still sees only current versions, so no answer can cite replaced content.

What is deliberately *not* built is the temporal query itself — *"what was the previous rate?"* — but the data to answer it now accumulates, and no further migration is needed to build it.

Verified on live Postgres: with v1 current, `$500` stood and `$800` was superseded; cutting over to v2 swapped them; a re-run changed nothing; rolling back swapped them again. Migration `d5a91c3f7b28`. Full analysis in [`ingestion.md`](ingestion.md) §5 item 5.

---

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

### ✅ P0-3 — No authentication anywhere — **FIXED 2026-09-08**

**Was:** every one of the 13 endpoints anonymous, `allow_origins=["*"]`, and `/ingest/crawl` willing to fetch any URL a caller named — including `http://169.254.169.254/`, which is a server-side request forgery yielding cloud credentials in one request. `_uploaded_by()` hardcoded the service-account UUID, so nothing was attributable to anyone.

**Now:** JWT (HS256) with a 15-minute access token and a 14-day *rotating* refresh token whose id is stored so it can be revoked; `HttpOnly; Secure; SameSite=Strict` cookies for the browser and `Authorization: Bearer` for scripts, over one verifier; Argon2id passwords; two roles, `member` and `admin`. Design and rationale: [`authentication_implementation.md`](authentication_implementation.md).

The full design decisions live in that document. Four things worth having here:

**`/chat` is authenticated.** This is an internal tool, and every turn spends Groq tokens against a daily budget that this project's own testing exhausted repeatedly with one developer. Authentication bounds who can spend it; a per-user daily cap bounds how much any one of them can. The public surface is now exactly three routes — `/health`, `/auth/login`, `/auth/refresh` — and `tests/api/test_route_protection.py` enumerates the assembled app and asserts every other route answers 401 without a credential. That test exists to catch the endpoint somebody adds in six months and forgets to protect, which no per-endpoint test can.

**`/graph/*` is not public despite being read-only.** Those six endpoints walk the entire knowledge graph, which makes them a far more efficient way to exfiltrate the corpus than asking the chatbot five hundred questions. Read-only means it does not write, not that it is harmless.

**The SSRF guard sits at the crawler's fetch boundary, not only at the API.** A site is crawled by following its links, so a link on a public page pointing at an intranet host is fetched by the same code path. `ingestion/crawler/fetcher.py` is the single network I/O boundary for page and document fetching, so the check goes there and covers discovery, `robots.txt`, `sitemap.xml`, downloads and every followed link. Redirects are walked one hop at a time — `follow_redirects=True` was the bypass, since a public URL that 302s to the metadata endpoint sailed past a check on the URL the caller supplied.

**Two security-relevant writes needed explicit commits.** Revoking every session on detecting a replayed refresh token, and incrementing the login rate-limit counter, both happen on the way to an error response — and `get_session` rolls back when a handler raises. Without the commits, the response to a *detected token theft* was silently undone, and the fifth wrong password was as unthrottled as the first. Both were found by tests that asserted the consequence rather than the status code.

**Migration:** `8e5a3c9d21f7` — four columns on `app_user` (`password_hash`, `role`, `is_active`, `last_login_at`), plus `refresh_token` and `rate_limit_bucket`. Additive; existing rows become active members and the service account is promoted to `admin` because `uploaded_by` points at it.

**Known limitation, written down rather than papered over:** the guard cannot pin a connection to the address it validated — `httpx` has no supported way to do it, and the workaround breaks certificate verification. It instead reads the peer address off the completed connection and rejects the response before any of it is used. An attacker can still cause one *blind* request to an internal address; they cannot see the answer.

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
| `auth/jwt.py` | Deleted. It was 0 bytes; an empty file is not a plan. **P0-3 replaced it with a real `auth/` package on 2026-09-08** — `tokens.py`, `passwords.py`, `roles.py`, `dependencies.py`, `router.py`, `cookies.py`, `rate_limit.py`, `ssrf.py`. |
| `output/docs.md` | Deleted and `output/` gitignored — a tracked crawl artefact from `example.com`. |
| `frontend/app/.vite/deps_temp_*` | Deleted and `**/.vite/` gitignored. |
| **Committed debug values** | `CrawlConfig.request_timeout` 520.0 → **15.0** and `max_pages` 5 → **50**, the values from before commit `02b41a5`. A 520-second per-request timeout stalls a crawl for nearly nine minutes on one unresponsive page; `max_pages=5` silently truncated every site crawl to five pages. |
| `README.md` | Rewritten. **The report was wrong about this one**: it was not the one-line `# AI Customer Assistant`, it contained a single stray absolute path. It is now real setup documentation — requirements, first run, the two-`.env` split, every make target, ingestion, tests and layout. The "there is no authentication" warning it carried was removed on 2026-09-08 and replaced by an Authentication section, because P0-3 made it false. |
| `.pytest_cache` | Removed from disk (already gitignored). |
| Branches | **Deliberately not touched.** 13 local + 24 remote branches remain. Deleting branches is irreversible and is the user's call, not a hygiene sweep's. |

`config.py` is no longer 0 bytes — P0-2 gave it `load_env()`.

---

## 7. Recommended improvements

### 7.1 Correctness & safety (do first)
1. ~~Fix the ticket flow (P0-1).~~ **Done 2026-09-05** — 7 regressions cleared, 11 tests added.
2. ~~Remove import-time `load_dotenv()` (P0-2).~~ **Done 2026-09-06**
3. ~~Implement JWT auth + CORS allowlist + SSRF guard on the crawler (P0-3).~~ **Done 2026-09-08.**
4. ~~Replace `print()` node logging with redacted structured logging (P0-4).~~ **Done 2026-09-06**
5. ~~Fall back to vector search when a structured-only lookup returns nothing (P1-6).~~ **Done 2026-09-06**
6. ~~**Decide the superseded-content policy (P1-7)**~~ **Done 2026-09-10.** The decision taken was to keep history and mark it: `value_provenance` records which version asserted each fact, `value.superseded_at` marks the ones no current document still asserts, and structured lookup returns current values only. Chunk search is unchanged by choice — `STALE` versions stay out of semantic search, so no answer can cite replaced content. What is deliberately *not* built is the temporal query itself ("what was the previous rate?"); the data to answer it now accumulates.
7. Mark DB/Playwright tests with `@pytest.mark.integration` and add a `-m "not integration"` default so the unit suite is green on a clean checkout.

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
17. ~~**Ticket status lookup**~~ **Done 2026-09-09** — `TicketStore.get_ticket` plus a `ticket_status` graph node. What it deliberately does not do is find a ticket from an email address; see §3.
18. ~~**Admin API (read)**~~ **Done 2026-09-09** — `api/admin.py`. The long-standing note pointing at `ingestion/storage/api.py` was aimed at the wrong module: that is a *write* path duplicating `POST /ingest/upload`, and wiring it would have produced a second upload route while leaving all four read tabs empty. What remains is the **write** surface: delete a source, trigger a re-index, change a ticket's status.
19. **Prompt management endpoint** so the Prompt page's edits persist server-side and are versioned.
20. ~~**Streaming `/chat`** via SSE.~~ **Done 2026-09-06 (F7)** — `POST /chat/stream`; the buffered endpoint remains as the fallback.
21. **Ticket lifecycle** — `updated_at`, `resolved_at`, assignee, a `thread_id` FK linking a ticket back to its conversation, and inbound email replies.

### 7.5 Operations
22. Structured JSON logging. (Log *levels* were fixed with F5; the `trace_id` column was added 2026-09-10 and ingestion fills it, so what remains is JSON structure and setting the same context on the chat path, which still logs `-`.)
23. Prometheus metrics: chat latency by stage, retrieval hit rate, LLM token spend, job queue depth.
24. `/health` should check Postgres, MinIO and Tika — it currently returns `{"status": "ok"}` unconditionally.
25. ~~A stale-job reaper.~~ **Done 2026-09-05** with P1-5.
26. Move secrets to a secret manager; `backend/.env` currently holds a live `GROQ_API_KEY`, an SMTP password and now `AUTH_SECRET` in plaintext on disk (correctly gitignored, but not protected). `AUTH_SECRET` raises the stakes: it signs every session, so leaking it is equivalent to leaking every password at once. Rotating it signs everyone out, which is the intended emergency response.
28. **Sweep `refresh_token` and `rate_limit_bucket`.** Both accumulate rows that expire on their own but are never deleted. `auth.rate_limit.sweep_expired` exists and has no caller; the equivalent for refresh tokens is a one-line `DELETE ... WHERE expires_at < now()`. Low urgency at this volume, and worth doing before it is not.
27. ~~Write the README.~~ **Done 2026-09-06** with P2-6 — requirements, first run, the two-`.env` split, make targets, ingestion, tests and layout. An architecture diagram is still missing.

---

## 8. Quick wins (under an hour each, high value)

- [x] ~~Add the missing `f` to the email subject~~ — **done** (P0-1)
- [x] ~~`git rm echo backend/store_new.py backend/src/ai_customer_assistant/api/chat.py`~~ — **done** (P2-6)
- [x] ~~Delete the `?` splitting block~~ — **done** (P1-3)
- [x] ~~Guard `_log_node` behind an environment check~~ — **done** (P0-4), and it redacts rather than merely gating
- [x] `allow_origins` from an env var — `main.py` (P0-3, done 2026-09-08; the default is now *no* cross-origin access at all, which restricts nothing, because the frontend is served same-origin)
- [x] ~~Revert `CrawlConfig.request_timeout` to 15.0 and `max_pages` to 50~~ — **done** (P2-6)
- [x] ~~Add `output/`, `frontend/app/.vite/` to `.gitignore`~~ — **done** (P2-6)
- [x] ~~Use `self._session_factory` in `TicketStore.create_ticket`~~ — **done** (P0-1)
- [ ] Make `/health` check the database
- [x] ~~Write a real README~~ — **done** (P2-6)
- [x] ~~Log the swallowed exception in `classify_and_route`~~ — **done** (F3); `node.py:104` now logs with `exc_info=True`, and a provider rate limit is worded as a wait rather than a breakage.
- [ ] Sweep expired `rate_limit_bucket` and `refresh_token` rows — `auth.rate_limit.sweep_expired` is written and has no caller.
- [ ] A "sign out everywhere" button — the code path exists (it fires on detected token reuse), with nothing attached to it.

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

**Create an account** (there is no self-signup; the first admin has to come
from here, because `POST /auth/users` requires one to already exist):
```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH ./.venv/bin/python scripts/create_user.py you@example.com --role admin
```

**Apply migrations from the host** (the compose Postgres is published on 5433):
```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH PYTHONPATH=src/ai_customer_assistant ./.venv/bin/python -m alembic upgrade head
```

**Codebase size:** backend `src` ~21 600 lines · tests ~14 200 lines · frontend ~4 800 lines. The test suite more than tripled over this work — 268 → **969 passing**.

**Largest modules:** `frontend/src/pages/graph.js` (930) · `db/models.py` (687) · `ingestion/extraction/agent.py` (615) · `ontology/vocabulary.py` (596) · `services/chat_service.py` (558) · `ingestion/pipeline.py` (541).

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
| `KNOWLEDGE_AGENT_TOP_K` | 8 (**set to 4**) | Must not exceed `MAX_CONTEXT_CHUNKS`. **Measured floor is 4**: 8, 6 and 4 all give recall@k 100% / MRR 0.933 on the golden set while mean chunks fall 5.2 → 3.4; 3 breaks it (96.15%). The answer prompt is the largest line item in a turn's token cost, so this is the cheapest latency lever |
| `KNOWLEDGE_AGENT_MAX_CONTEXT_CHUNKS` / `MAX_STRUCTURED_FACTS` | 12 / 20 | Prompt budgets |
| `KNOWLEDGE_AGENT_EXTRACTION_CONFIDENCE_THRESHOLD` | 0.55 | No longer used for routing (F5); still gates extraction |
| `KNOWLEDGE_AGENT_LLM_PROVIDER` / `LLM_MODEL_NAME` / `REWRITE_MODEL_NAME` | anthropic / claude-sonnet-5 | Groq, Anthropic and Gemini all work end to end (classifier *and* Knowledge Agent). Resolution is: this setting if its key exists, then the Admin page's default, then any provider with a key, then the stub. **The split model fields are only honoured when `LLM_PROVIDER` names the provider that actually resolves** — otherwise that vendor's own default model is used, so a config tuned for one vendor never leaks model names into another's API |
| `EMBEDDING_QUERY_INSTRUCTION` | *model-derived* | Overrides the BGE query prefix; set to empty to disable it |

**Authentication** (P0-3, `auth/`):

| Variable | Default | Notes |
|---|---|---|
| `AUTH_SECRET` | *none* | **The one setting with no default.** The process refuses to start without it, and rejects anything under 32 characters. A defaulted signing key means anyone who has read the source can mint tokens for any account. Rotating it signs everyone out — which is the intended emergency response |
| `AUTH_COOKIE_SECURE` | `true` | Leave on. `http://localhost` still works; browsers treat it as a trustworthy origin. Set `false` only to reach the app over plain HTTP at a LAN address, and understand that it sends session tokens in clear |
| `AUTH_COOKIE_SAMESITE` | `strict` | Available *because* the frontend is same-origin, and what makes CSRF a non-problem |
| `CORS_ALLOW_ORIGINS` | empty | Empty means no cross-origin access at all, which restricts nothing — the frontend is same-origin. A wildcard is **not** accepted: the session is a cookie, and the CORS spec forbids combining credentials with `*` |
| `TRUST_PROXY_HEADERS` | `false` | Read `X-Forwarded-For` for rate-limit keys. Only behind a proxy that sets it — anyone can send the header, so trusting it without one lets a caller choose their own rate-limit key |
| `RATE_LIMIT_DISABLED` | `false` | Tests and single-user local development only |
| `CHAT_GLOBAL_PER_DAY` | 400 | The deployment-wide chat ceiling. Chat is public, so per-caller limits do not bound the provider bill — this one does. Anonymous callers are rate-limited by client IP; signed-in ones by user id |

**LLM provider keys** (`llm_credentials.py`, and the Admin › API Keys page):

| Variable | Default | Notes |
|---|---|---|
| `GROQ_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | unset | Still read, and still the fallback. A key saved through the Admin page **shadows** the matching variable; clearing it falls back here rather than switching the provider off |

The stored keys are encrypted under a key derived from `AUTH_SECRET`, so
**rotating that secret means re-entering every provider key**. There is no
separate encryption secret on purpose: a second one is a second thing nobody
remembers to rotate.

**Crawler safety** (`auth/ssrf.py`):

| Variable | Default | Notes |
|---|---|---|
| `CRAWL_DOMAIN_ALLOWLIST` | empty | Restrict crawling to these domains, subdomains included. Empty means any publicly-routable host |
| `CRAWL_ALLOW_ADDRESSES` | empty | CIDRs exempt from the private/loopback/link-local refusal, for crawling an intranet or for the crawler's own fixture site on loopback. A **list of networks, not a switch**, so exempting `10.1.0.0/16` does not also exempt `169.254.169.254` — which would hand an authenticated caller the instance's credentials |

**Logging and privacy:**

| Variable | Default | Notes |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Uvicorn leaves the root at WARNING, so this is what makes application logs appear at all |
| `LOG_PII` | unset (off) | Opt back in to un-redacted node traces. **Never set in a deployed process** — see P0-4 |

**Database pool** (`db/engine.py`): `DB_POOL_SIZE` (10), `DB_MAX_OVERFLOW` (5), `DB_POOL_TIMEOUT` (30), `DB_POOL_RECYCLE` (1800).

**Credentials and services:** `GROQ_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY`, `POSTGRES_*`, `MINIO_*`, `TIKA_BASE_URL`, `FRONTEND_DIR`. `INGEST_DEFAULT_USER_ID` still exists but no longer applies to HTTP requests: since P0-3 an upload is attributed to the caller, and the fallback is reached only by the worker and the offline scripts, which genuinely have no person behind them.

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

What was missing was the last mile. The system was built module-by-module against a plan document, and the seams showed where the modules met: the ticket flow half-migrated to async and broke, the two ontologies forked and drifted, four database engines accumulated, the timeout budgets contradicted each other across the client/server boundary, state that had to be shared was left in per-process dictionaries, and the retrieval graph had a branch with no way out. All of those are fixed, and so is the largest of the cross-cutting concerns no single module owned: **authentication**. What is still absent is **structured logging and CI**.

A pattern worth naming, because it recurs and it is the hardest kind of defect to notice: **most of the bugs found here failed silently rather than loudly.** A threshold rejected four-fifths of answerable questions. An idempotency guarantee held only inside one process. A timeout abandoned requests the server went on to complete. A classifier swallowed every exception and returned a friendly apology. A retrieval branch skipped the only search that would have worked. In each case every component behaved exactly as written, so nothing errored and no log line appeared — the damage was only visible in the relationship *between* components, or by measuring the output against what it should have been.

P1-6 is the sharpest illustration, and the reason it is worth changing how this codebase is tested. Every module involved was individually correct and individually well tested; the defect was one missing edge in the graph that connects them. Unit tests could not have found it, and did not. The fixes that catch this class of bug are the ones that assert across boundaries: the golden set that measures retrieval end to end, the ladder assertion in `timeouts.py`, the ontology drift tests, the compiled-graph tests added with P1-6, and now the route-protection test that enumerates the assembled application rather than trusting each router to have remembered.

P0-3 produced two more of exactly this shape, worth recording because neither would have shown up as an error. Revoking every session on detecting a replayed refresh token, and counting a failed login attempt, both happen on the way to an *error response* — and the request-scoped session rolls back when a handler raises. Both writes were silently discarded: theft detection revoked nothing, and the fifth wrong password was as unthrottled as the first. Every line of both functions was correct; the defect lived in their relationship with the framework's error handling. They were caught only by tests that asserted the **consequence** — count the live tokens, read the counter — rather than the status code the endpoint returned.

**All ten P0/P1/P2 items and all nine live-test defects are now closed. This is a deployable internal product.** What remains is unbuilt features (the admin write API, prompt management) and operational polish (structured logging with the `trace_id` already threaded through every node, metrics, CI) — work that adds capability rather than work that removes risk.

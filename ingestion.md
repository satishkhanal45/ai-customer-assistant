# The Ingestion Pipeline — how it works, and where it hurts

**Status:** analysis, and the P0 tier is now **fixed** (2026-09-09) — see
§5. The rest stands as written.
**Date:** 2026-09-09
**Measured against:** the live database on this machine — 24 knowledge
sources, 24 versions, 82 ingestion jobs, 125 chunks, 339 entities, 242
relations.

---

## The headline

**60 of the 82 ingestion jobs in your database failed. That is a 73%
failure rate**, and it is not random — it is two specific, fixable causes:

| Failure | Count | Cause | State |
|---|---:|---|---|
| `persist_failed: Multiple rows were found when exactly one was required` | **31** | A real bug. `persist_chunks` was not idempotent, and nothing in the schema stopped it | ✅ **Fixed 2026-09-09** |
| `eav_extraction_failed` | **29** | Groq 429 rate limits and 400 JSON-validation errors, with **no retry anywhere in the pipeline** | ✅ **Closed 2026-09-09** — §5 items 6, 8, 8b |

Both are diagnosed in §4. Neither is a design flaw in the architecture,
which is sound; both are gaps in the seams between good components.

**The first is now closed.** `persist_chunks` replaces rather than appends,
`uq_chunk_version_index` makes the duplicate state unrepresentable, and
migration `b7d1e93a5c40` cleaned the existing rows — **58 redundant chunks
across 31 duplicated indexes**, 125 rows down to 67. Verified against the
live database: three consecutive re-ingests of one version leave four
chunks, not twelve.

**The second is now closed too.** The 429s needed quota and fewer calls
(§5 item 8 batched them, cutting calls per document by ~60%); the 400s needed
§5 item 8b, which stopped retrying a deterministic rejection five times and
made a rejected batch split rather than fail its document. **As of 2026-09-09
all 24 sources are indexed** — 77 chunks, 491 entities, 768 values, 374
relations — which had never previously been true.

What remains is not a failure *cause* but a failure *policy*: nothing retries
a transient failure later, and nothing distinguishes a job that will fix
itself from one that needs a human. That is §5 items 6 (retry half) and 7,
now the top of the order of work in §7.

---

## 1. What ingestion is

Turning a document into two things the assistant can search:

1. **Embedding chunks** — ~500-token windows with a 768-dimension vector
   each, in `embedding_chunk`, searched by pgvector.
2. **An entity–attribute–value graph** — `entity` / `attribute` / `value` /
   `relation`, extracted by an LLM, searched by exact structured lookup.

A question that names something the graph knows searches both at once. So
ingestion quality sets the ceiling on answer quality: a document that fails
to ingest is a question the assistant cannot answer, and it fails silently —
retrieval simply returns nothing.

---

## 2. The two entry paths

Everything converges on one queue table, but content arrives two ways.

```
  POST /ingest/upload              POST /ingest/crawl
  (multipart PDF/DOCX/MD)          (a URL, or a site discovery)
          │                                  │
          │                          crawler: Playwright fetch,
          │                          trafilatura → markdown
          │                                  │
          └──────────────┬───────────────────┘
                         ▼
          register_document_version()         ← queue/document_producer.py
                         │
             ┌───────────┴────────────┐
             │  checksum classify     │
             │  · already INDEXED  → skip entirely
             │  · unfinished       → reuse the version, re-upload, requeue
             │  · new              → new source and/or version row
             └───────────┬────────────┘
                         ▼
              MinIO put_object(raw bytes)
                         ▼
        INSERT knowledge_injection_job (status=QUEUED)
                         ▼
                    202 Accepted
```

**The API only enqueues.** It does not ingest. This is deliberate and was
fixed in P1-5: the endpoints used to `asyncio.create_task` the pipeline
inside the web process, which ran the 400 MB embedding model in a request
worker, dropped the task reference, and orphaned in-flight jobs on restart.

**Deduplication is by content checksum**, and it is three-way rather than
binary — the distinction that matters is between "we already have this"
and "we tried and did not finish", because only the first is a real
duplicate.

---

## 3. The pipeline, stage by stage

The worker (`scripts/run_worker.py` → `queue/worker.py`) polls every 2
seconds, claims **one** job, and runs it to completion before claiming
another.

```
claim_next_job()                                  queue/repository.py
  · SELECT … WHERE status='QUEUED'
      AND NOT EXISTS (RUNNING job for this source)   ← one-job-per-source
      ORDER BY started_at NULLS FIRST
      FOR UPDATE SKIP LOCKED LIMIT 1
  · flip to RUNNING and COMMIT immediately
        │
        ▼
run_ingestion()                                        pipeline.py
        │
   ┌────┴─────────────────────────────────────────────────────────┐
   │ 1. fetch_bytes      MinIO get_object, verify checksum        │
   │ 2. extract_text     Tika /rmeta/text  → plain text           │
   │ 3. load reuse map   previous version's checksum→embedding    │
   │ 4. chunk_and_embed  500-token windows, 75 overlap, BGE       │
   │ 5. eav_extraction   ONE LLM call PER CHUNK, serially         │
   │ 6. persist          chunks, then entities/facts/relations    │
   │ 7. cutover          point source.current_version_id at it    │
   └────┬─────────────────────────────────────────────────────────┘
        │  Ok  → version INDEXED, job SUCCEEDED (+ counts)
        │  Err → session.rollback(), version FAILED, job FAILED
        ▼
complete_job()  — status, counts, error_details
```

Each stage is `(deps) -> async (ctx) -> Result`, folded by
`run_pipeline_async`, short-circuiting on the first `Err`. Every I/O
boundary is an injected callable, so the pipeline is testable without
Postgres, MinIO, Tika or an LLM. **This is the best part of the design** and
none of the recommendations below disturb it.

### Details worth knowing

- **Chunking**: 500 tokens, 75 overlap (15%). Measured on your live data:
  125 chunks, mean 441 tokens, max 500 — the windowing is behaving.
- **Embedding reuse**: on a re-ingest, chunks whose text checksum matches
  the previous version reuse that embedding instead of recomputing. Real
  saving on a document where one paragraph changed.
- **Failure rolls back** (P2-3): `_stage_persist` writes with `flush` and no
  commit, so an error after it would otherwise carry partial chunks into the
  database alongside the FAILED status.
- **Stale-job reaping**: a worker killed mid-job leaves a row `RUNNING`
  forever, and the one-job-per-source guard would then block that source
  permanently. `reap_stale_jobs` requeues anything RUNNING for >30 minutes,
  once, at worker startup.

---

## 4. What is actually going wrong

### 4.1 `persist_chunks` is not idempotent — 31 failures

**The bug.** `persistence.persist_chunks` unconditionally `session.add()`s a
row per chunk. It never deletes what is already there for that version. So
ingesting the same `version_id` twice writes every chunk twice.

Then `_link_entity_to_chunk` does this:

```python
chunk = (await session.execute(
    select(EmbeddingChunk).where(
        EmbeddingChunk.version_id == version_id,
        EmbeddingChunk.chunk_index == chunk_index,
    )
)).scalar_one()          # ← raises the moment there are two
```

**The evidence.** Duplicate `(version_id, chunk_index)` rows are present in
your database right now — one index has **seven** copies:

```
              version_id              | chunk_index | count
--------------------------------------+-------------+-------
 bba2c47f-5e0f-4ce0-8a68-ba2048901f66 |           2 |     7
 253fbe51-b4ac-46ca-aab8-83e664c8a476 |           1 |     3
```

And there is **no unique constraint** on `(version_id, chunk_index)` to stop
it — the only indexes on `embedding_chunk` are the primary key, checksum,
entity_id, version_id and the HNSW vector index.

**Why it repeats.** One version has **14 jobs** against it. Every version
with duplicated chunks has exactly one SUCCEEDED job and several FAILED
ones. Once a version has duplicate chunks the state is *permanent*, so every
subsequent attempt fails the same way. It is a self-perpetuating failure:
the document can never be ingested again without manual database surgery.

**Severity: this is the single highest-value fix in the pipeline.** It
accounts for over half the failures and it poisons documents permanently.

### 4.2 No retry, anywhere — 29 failures

The extraction stage makes **one LLM call per chunk, serially**:

```python
return tuple(
    extract_chunk(agent, source_name=…, chunk_index=…, chunk_text=…)
    for embedded in chunks
)
```

An 8-chunk document is 8 sequential Groq calls. The observed failures:

- `Error code: 429 — Rate limit reached for model openai/gpt-oss-120b`
- `Error code: 400 — Failed to validate JSON`

A 429 is *transient by definition*. The pipeline treats it as terminal: the
job is marked FAILED and nothing ever tries again. The `Result` type even
distinguishes `tika_transient` from `tika_extraction_failed` — the
information is there and nothing acts on it.

Compounding it: **ingestion and chat share one Groq quota.** A worker
draining a backlog will both fail its own jobs and make the assistant answer
*"I'm handling more requests than I can keep up with"*.

### 4.3 The heavy models are reloaded on every job

`run_ingestion` calls `_resolve_deps(session)` whenever `deps is None`, which
is every call from the worker. That function constructs a `StorageClient`, an
`httpx.Client`, and:

```python
tokenizer = get_tokenizer(chunk_settings.embedding_model_name)
embedding_model = get_embedding_model(chunk_settings.embedding_model_name)
```

Both loaders document that they are deliberately *not* memoized —
"calling this twice loads the model twice". So the 400 MB BGE model is
loaded from disk **once per document**.

Meanwhile `pipeline.py`'s own module docstring says:

> `tokenizer/embedding_model are loaded ONCE and injected via PipelineDeps,
> not reloaded per job`

That is not what the wiring does. The intent was right; the seam between
`run_ingestion` and the worker loop lost it.

### 4.4 Smaller things that are still real

- **The `httpx.Client` is never closed.** `_resolve_deps` creates one per
  job and nothing calls `.close()`. A file-descriptor leak proportional to
  jobs processed.
- **The EAV agent bypasses the credential store.** `_default_extraction_agent`
  reads `os.environ.get("GROQ_API_KEY")` directly, so a key set on the new
  **Admin › API Keys** page is used by chat but **not** by ingestion. Two
  sources of truth for one credential.
- ~~**The ingestion LLM call has no timeout, and the worker is serial.**~~
  **Timeout fixed 2026-09-09** (see §5 item 6). The serial worker remains.
  Original finding:
  Found on 2026-09-09 while re-running the failed jobs against a fresh Groq
  key: one document sat in `RUNNING` for **28 minutes** with no log output
  and no progress, and because the worker finishes one job before claiming
  another, the whole queue stopped behind it. `_default_extraction_agent`
  builds `Groq(api_key=…)` with no timeout override, and nothing in
  `pipeline.py` bounds the stage — unlike the chat path, which has a full
  budget ladder in `timeouts.py`. The stale-job reaper does not rescue it
  either: it runs *once at worker startup*, so a worker that is still alive
  and stuck blocks indefinitely. **A per-stage timeout would have turned a
  stalled queue into one failed job.**

  *Confirmed by the fix.* With a 30s client timeout, `max_retries=3` and a
  600s stage budget, the same document produced a clean
  `eav_extraction_timeout` and the queue drained instead of stopping — three
  stalls became three labelled failures. What the timeout does **not** do is
  make those documents ingestable; see item 8.
- **No attempt limit and no dead-letter.** A permanently poisoned version is
  retried forever; nothing marks it as beyond help.
- **`reconcile.py` — 366 lines with no caller.** A one-time data
  reconciliation with no CLI entry point, no Make target, and no test. It is
  either dead code or an undocumented manual procedure.
- **`error_details` is free text.** Diagnosing the failures for this document
  needed `split_part(error_details, ':', 1)`. The failure *kind* is
  structured information stored as prose.

---

## 5. Room for improvement, in priority order

Ordered by evidence and value, not by effort.

### ✅ P0 — stop the bleeding — **DONE 2026-09-09**

*All four shipped together with migration `b7d1e93a5c40` and nine tests in
`tests/ingestion/test_chunk_persistence.py`. Suite 798 → 807 passing.*

**1. Make chunk persistence idempotent.**
Delete the chunks belonging to **this same `version_id`** before inserting,
so re-ingesting a version replaces rather than appends.

*This loses nothing.* Chunks belong to a version, not to a source, and a
version is an immutable snapshot — the pipeline verifies at stage 1 that the
bytes still match the version's recorded checksum, and fails with
`checksum_mismatch` if they do not. Re-ingesting v3 replaces v3's chunks with
chunks derived from identical bytes. Superseded versions (v1, v2) have
different `version_id`s and are untouched; `cutover` only marks them `STALE`.
Concretely, the failures in §4.1 come from a version holding *seven copies of
chunk 2* — deleting six identical rows loses no information.

The separate question of whether *old versions* should remain answerable is
item 5 below, and it is a real gap — but it is a retrieval question, not a
reason to keep duplicate chunks.

**2. Add a unique constraint on `(version_id, chunk_index)`.**
Make the invalid state unrepresentable rather than relying on every writer
to behave. It also turns a future regression into an immediate, obvious
integrity error instead of a confusing `scalar_one()` failure three
functions away.

**3. Clean up the existing duplicates.**
A data migration keeping the lowest `chunk_id` per `(version_id,
chunk_index)`. Without this, every already-poisoned document stays
permanently unignestable even after fixes 1 and 2 ship.

**4. Replace `scalar_one()` in `_link_entity_to_chunk`.**
Defence in depth. Even with the constraint, a lookup that can only ever
match one row should say so with `.first()` and an explicit check rather
than by raising a message that names neither the version nor the chunk.

**What shipped, and two things found while shipping it.**

The four changes went in as described. Two portability defects surfaced on
the way, both in `db/models.py` and both invisible on Postgres:

* `embedding`'s SQLite variant was `Text`, which cannot bind a Python list —
  so any test that actually *wrote* a chunk failed. It is `JSON` now, which
  round-trips the vector and lets the persistence tests exercise the real
  `INSERT` instead of mocking the one statement they are about.
* `chunk_id` had only `server_default=func.gen_random_uuid()`, which SQLite
  does not have. It now carries a Python-side `default=uuid.uuid4` as well,
  the same pairing `AppUser.id` already uses.

Neither changes anything on Postgres. Both are the reason this code had no
test before: the table could not be created, let alone written to, off
Postgres — which is exactly the gap the bug lived in.

**Verified on the live database**, not only in tests: three consecutive
re-ingests of one version leave four chunks with zero duplicate groups, and
a subsequent two-chunk re-ingest leaves two — no orphaned tail from the
longer run.

### P1 — reliability and correctness over time

**5. Decide what happens to superseded content — and make the two halves
agree.** *(Raised by the question "if the rate changed, can the assistant
still tell me what it used to be?")*

Today the two retrieval paths answer that question in opposite, and both
wrong, ways:

* **Chunks keep history and hide it.** `cutover` marks the old version
  `STALE` and deletes nothing, so every superseded chunk is still in the
  database. But `vector_search.py` filters
  `WHERE knowledge_source_version.version_id = knowledge_source.current_version_id`,
  so semantic search can never reach them. *"What was the previous rate?"* is
  unanswerable, and the data needed to answer it is sitting right there.
* **Facts keep history and cannot distinguish it.** `value` rows are written
  with `ON CONFLICT DO NOTHING` on `(entity_id, attribute_id, value)`. When a
  price changes from $500 to $800, **both rows survive**, under the same
  entity and attribute, with no timestamp and no version link. Structured
  lookup returns both, and nothing marks which is current.

The second is the more serious of the two: a missing answer is visibly
missing, whereas two contradictory prices presented as equally true is a
*wrong* answer delivered confidently.

Not yet visible in this deployment — all 125 chunks currently belong to
current versions, because nothing has been re-ingested with changed content
yet. It becomes real the first time a document is updated.

Two coherent positions, and the work differs:

* **History matters.** Chunks carry their version into retrieval, temporal
  questions are allowed to reach `STALE` versions, and every answer says
  which version a fact came from. `value` gains a version reference so
  "current" is derivable. This is a feature, not a fix.
* **Only current truth matters.** Re-ingest supersedes old values rather
  than accumulating them, and superseded chunks are pruned on a retention
  schedule.

Either is defensible. The present state — half of one and half of the other
— is not. **This needs a product decision before it needs code**, which is
why it is listed first in this tier but last in the suggested order of work.

**6. Bound the extraction stage with a timeout, and retry transient
failures with backoff.**
Two halves of the same problem.

*Timeout first*, because it is the cheaper fix and the one that keeps the
queue moving: give the Groq client an explicit timeout and give the stage a
wall-clock budget, the way `timeouts.py` already does for chat. Without it a
single hung call stops every remaining document (§4.4).

*Then retry*: add `attempt_count` and `next_attempt_at` to
`knowledge_injection_job` and retry `tika_transient` and 429 with
exponential backoff up to a small limit (3–4). The `Result` type already
distinguishes transient from terminal; the queue just needs to honour it.

**Measured 2026-09-09**, re-running the 15 failed documents against a fresh
Groq key: rate-limit *failures* went to **zero** — the Groq SDK's own backoff
absorbs them once quota exists — and the residue was three `400 Failed to
validate JSON` errors, which are *not* transient. So the retry work should
be scoped to transient classes only, and the 400s treated as a separate
prompt/schema problem rather than something to retry into.

**The timeout half shipped the same day.** `INGEST_EXTRACTION_CALL_TIMEOUT_S`
(30s, handed to the Groq client along with `max_retries=3`) and
`INGEST_EXTRACTION_STAGE_BUDGET_S` (600s, enforced around the stage) now live
in `timeouts.py` beside the chat ladder, in their own clearly-separated
section — ingestion is background work and is *allowed* to be slower than a
chat turn, but not unbounded. A new `eav_extraction_timeout` failure kind
distinguishes "this never came back" from "the model refused this". Six tests
in `tests/ingestion/test_extraction_timeout.py`.

*Honest limitation, written into the code:* `asyncio.wait_for` abandons the
await but cannot kill the thread `to_thread` is running, so the orphaned call
continues until the **client** timeout ends it. That is why both bounds
exist, and why the client one is primary.

**What it did and did not achieve.** Re-running the last three documents:
all three produced clean `eav_extraction_timeout` failures and the queue
drained, where before one of them had stalled the worker indefinitely. But
they are still not ingested — they are large documents, extraction makes one
serial LLM call per chunk, and against a free-tier per-minute token budget
that cannot finish inside any sane wall clock. **The timeout converted an
invisible hang into a visible, correctly-labelled failure; item 8 is what
would actually ingest them.**

**7. Cap attempts and dead-letter the rest.**
A job that has exhausted its retries should reach a terminal state that is
visibly different from "failed once" — otherwise the Admin › Jobs page
cannot distinguish "will fix itself" from "needs a human".

**8. Reduce the LLM calls per document.** ✅ **Done 2026-09-09.**

It was worse than one call per chunk. Chunks longer than 1800 characters are
split into windows and **each window was its own call** — so a 5-chunk
document made 10 calls, not 5.

*Measured before changing anything:* the system prompt carries the whole
canonical vocabulary and is **896 tokens**; a window of text is about 450. So
**two thirds of every call was the same text sent again**, and on an 8,000
tokens-per-minute budget that overhead is what decides whether a document
finishes.

Windows from every chunk are now flattened, grouped into batches of three,
and each batch is one call; results map back by `window_id` and merge per
chunk. Verified live:

| Document | Windows | Calls before | Calls after |
|---|---:|---:|---:|
| `news` | 10 | 10 | **4** |
| `2` | 6 | 6 | **2** |
| `artificial-intelligence` | 6 | 6 | **2** |

Batching *across* chunk boundaries rather than within them matters: batching
within a chunk would leave a two-window chunk sending a batch of two, which
throws most of the saving away on short documents.

Three things fell out of the work:

* `INGESTION_EXTRACTION_BATCH_WINDOWS=1` restores exactly the old behaviour,
  one call per window — the escape hatch if a future model batches worse.
* A window the model omits yields an empty extraction and a warning, not a
  failed document. Losing one window's facts is the smaller harm, and the
  document is what the queue retries.
* `_MAX_COOLDOWN_WAIT` was **420 seconds inside a 600-second stage budget**,
  so a single rate-limit cooldown could consume 70% of the time available
  for a whole document and a second would exceed it outright. Now 60s,
  chosen against what is being waited for: Groq's per-minute bucket refills
  every minute, and a longer hint means the *daily* budget is gone, which no
  amount of waiting inside one job will fix.

Also removed: a second, identical `extract_chunk` that was immediately
shadowed by the windowing version below it — dead from the moment windowing
was added.

**What it did not fix.** The three documents still are not ingested. With
batching they now fail on the **daily** token budget (200,000 TPD, exhausted
by the day's repeated re-runs) rather than on per-minute throttling. That is
a quota ceiling, not a code problem: the jobs are left `QUEUED` and will
ingest on the next worker run once the day's budget rolls over. 14 tests in
`tests/ingestion/test_extraction_batching.py`.

**8b. A rejected batch must cost a window, not a document.** ✅ **Done
2026-09-09.**

Item 6 noted the `400 Failed to validate JSON` residue and set it aside as
"a separate prompt/schema problem". It was never given a work item, and it
turned out to be the *only* thing still blocking ingestion: with the P0 and
quota problems behind us, 22 of 24 sources were indexed and the two that were
not — `sdlc.pdf` and `tech_stck.pdf`, both at zero chunks — failed on nothing
else.

Two separate defects, found by reading the code rather than the doc.

*The retry predicate was retrying a deterministic failure.* `_is_retryable`
returned true on `"json" in lower`, and Groq's rejection message is *"Failed
to validate **JSON**"* — so every one of these was retried five times, at
`temperature=0`, with a byte-identical prompt, producing a byte-identical
rejection. Five times the tokens for a guaranteed failure, spent against the
200,000-token daily budget that was the binding constraint on finishing a
document at all. The clause was written for *flaky* JSON output; it was
catching a *deterministic* schema rejection. Retryability is now decided by
`_is_deterministic_rejection`, and rate limits remain retryable.

*Batching had tripled the blast radius of one bad call.* Item 8 records that
"a window the model omits yields an empty extraction, not a failed document"
— true, but that covers a window missing from an otherwise good response. A
call that is *rejected* raised straight out of `_extract_batch`, so one bad
call lost all three of its windows and failed the whole document. The
signature said what was really wrong: `failed_generation` came back **empty**,
which is a response outgrowing what the model will emit for three windows at
once, not content that cannot be extracted.

So a rejected batch is now split in half and retried, recursively. Repeating
the same request is never the answer to a deterministic rejection; a
*smaller* one can be. It also fixes the other case for free — if one window
genuinely cannot be extracted, the split isolates it and only that window is
lost.

Three boundaries, each of which is the difference between a fix and a new
bug:

* **Only deterministic rejections split.** Splitting a rate-limited batch
  makes two rate-limited calls against a budget that is already gone, and
  `_invoke_with_retry` has already waited out what waiting can fix. A 429
  still fails the document.
* **A document whose every window is rejected still fails.** Degrading to
  "extracted nothing" would mark the job `SUCCEEDED` with an empty graph —
  indistinguishable from a document that genuinely had no facts in it, which
  is worse than failing.
* **A single lost window is logged, with its chunk index.** The loss is
  visible rather than silent.

**Verified live, on the rejection this was written for.** Re-queued against
the rebuilt worker with quota available, `sdlc.pdf` produced exactly the
sequence the fix describes:

```
Extraction batch of 3 window(s) for sdlc.pdf was rejected (BadRequestError);
  splitting into 1 and 2 and retrying.
Extraction failed for a window of chunk 1 in sdlc.pdf
  (400 ... json_validate_failed ... 'failed_generation': ''); continuing
  without that window's facts.
Extracted sdlc.pdf with 1 of 9 window(s) lost.
```

The batch of three was rejected, split into 1 and 2, and the isolated single
window was *still* rejected and dropped while the other two extracted
normally. **One window lost instead of a document** — the same rejection
previously ended `sdlc.pdf` outright, which is why it had sat at zero chunks.

That the isolated window failed alone, at width 1, says both hypothesised
causes were real: one window here genuinely cannot be extracted, and the
other two were only collateral damage from sharing a call with it. The split
is what tells them apart. `tech_stck.pdf` hit no rejection at all and
succeeded straight through.

**All 24 sources are now indexed**, for the first time: 77 chunks (from 67),
491 entities (410), 768 values (708), 374 relations. 7 tests in
`tests/ingestion/test_extraction_batching.py`; suite 846 → **853 passing**.

**9. Route the EAV agent through `llm_credentials`.** ✅ **Done 2026-09-09.**
`_default_extraction_agent` read `os.environ["GROQ_API_KEY"]` directly, so a
key saved on the Admin › API Keys page switched *chat* over and left the
ingestion worker on the old one, with nothing anywhere saying so. It now
resolves through `llm_credentials.api_key_for("groq")`, which still falls
back to the environment — a deployment that never opens that page behaves
exactly as before.

*Estimated: a day and a half for items 6–9. Item 5 is a day once
the direction is chosen, and unbounded until it is.*

### P2 — efficiency and scale

**10. Load the tokenizer and embedding model once per worker process.**
✅ **Done 2026-09-09.**

`_resolve_deps` built everything per call and the worker calls it per job, so
the 400 MB BGE model and its tokenizer were loaded from disk **once per
document** — visible as `Loading weights: 199/199` on every job in the log.

A new `PipelineResources` holds the session-*independent* half (models,
storage client, HTTP client, extraction agent, Tika settings); it is built
once per process by `get_pipeline_resources()`, and `_resolve_deps` now only
binds the current session to it. `scripts/run_worker.py` builds it at startup
rather than letting the first job pay for it — which also means a broken
model or missing MinIO configuration fails at boot instead of turning the
first document into a mystery failure.

**Verified live:** three jobs claimed, **one** model load. Previously one
load per job.

**11. Close the `httpx.Client`.** ✅ **Done 2026-09-09**, as part of item 10 —
it lives in `PipelineResources` now, created once and closed by
`reset_pipeline_resources()` in the worker's `finally`. Previously one was
created per job and never closed: a file descriptor leaked per document.

**12. Consider concurrent jobs.**
The worker is strictly serial: claim one, finish, claim the next. The
`FOR UPDATE SKIP LOCKED` claim is already safe for multiple workers, and the
one-job-per-source guard already prevents two workers colliding on one
document. The blocker is memory — each worker process loads its own copy of
the embedding model — which is exactly why fix 9 comes first.

*Items 10 and 11 done. Item 12 remains; note that it needs more than this
cache — a `SentenceTransformer` is not safe to encode from several threads at
once, so concurrent jobs need a lock or a model per worker.*

### P3 — observability and hygiene

**13. Structure the failure reason.** A `failure_kind` column beside
`error_details` makes the Admin › Jobs page groupable and makes "what is
failing and why" a query rather than a `split_part`.

**14. Emit the `trace_id`.** It is threaded through the chat path and absent
from ingestion; a job cannot currently be correlated with its logs.

**15. Test the pipeline core.** `persistence.py` has no dedicated test, and
neither does the worker loop. `test_partial_commit_rollback.py` and
`test_stale_job_reaper.py` cover adjacent behaviour. The bug in §4.1 lives
in exactly the gap between them — a test that ingests the same version twice
would have caught it immediately.

**16. Resolve `reconcile.py`.** Give it an entry point and a test, or delete
it. 366 lines that nothing calls will rot.

**17. Update the stale docstrings.** `pipeline.py` still carries
"STILL TO CONFIRM", "ASSUMPTION", and the claim about model loading that
§4.3 shows is false. A comment that contradicts the code is worse than no
comment.

---

## 6. What is already good

Worth stating plainly, because the failure rate above is not an indictment
of the design:

- **The `Result`-composed stage pipeline** is genuinely well built. Every
  I/O boundary injected, short-circuit on first error, stages that are
  individually trivial to read.
- **The API/worker split** is correct, and was hard-won (P1-5).
- **The three-way checksum classification** — indexed / unfinished / new —
  is more thoughtful than most dedup implementations.
- **Embedding reuse across versions** is a real optimisation that many
  pipelines skip.
- **The stale-job reaper and the one-job-per-source guard** show someone
  thought carefully about what happens when a worker dies.
- **Partial-commit rollback** (P2-3) is exactly right and non-obvious.

The pipeline's problems are concentrated at the seams — persistence
idempotency, retry policy, and dependency lifetime — not in its structure.

---

## 7. Suggested order of work

*Revised 2026-09-09, after items P0 1–4, 6 (timeout half), 8, 8b, 9, 10 and
11 shipped. The ordering below is what is left, and the evidence for it is
the live database rather than the original triage.*

1. **P1 6 (retry half) + 7** — `attempt_count` / `next_attempt_at` on
   `knowledge_injection_job`, exponential backoff for transient classes only,
   and a terminal dead-letter state once attempts are exhausted. This is now
   the top item rather than the second, because item 8b removed the
   deterministic failures and **everything still failing is transient** — the
   remaining blocker on the last two documents is a daily token ceiling that
   a retry an hour later simply walks past. One migration, one worker change.
2. **P3 13** (`failure_kind`) alongside it: the same table, the same
   migration window, and it is what makes Admin › Jobs able to tell "will fix
   itself" from "needs a human" — which is the whole point of item 7.
3. **P3 15** (test the worker loop). `persistence.py` got its tests with the
   P0 work; the worker loop is the one real coverage gap left, and item 1
   above is about to change it.
4. **P1 5** (superseded content) whenever the product question behind it is
   settled — it is the only item here that needs a decision before it needs
   code, and it becomes urgent the first time a document is updated with
   changed content.
5. **P2 12** (concurrent jobs) and **P3 14** (`trace_id`) as they become
   convenient. Item 12 still needs more than the resource cache: a
   `SentenceTransformer` is not safe to encode from several threads at once.

A reasonable check that it worked: re-ingest the 24 existing sources and
expect a failure rate in the low single digits, with any remaining failures
carrying a `failure_kind` that says something more useful than
`persist_failed`.

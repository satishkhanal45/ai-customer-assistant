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
agree.** ✅ **Done 2026-09-10.** *(Raised by the question "if the rate changed,
can the assistant still tell me what it used to be?")*

The two retrieval paths answered that question in opposite, and both wrong,
ways:

* **Chunks kept history and hid it.** `cutover` marks the old version `STALE`
  and deletes nothing, but `vector_search.py` filters
  `WHERE knowledge_source_version.version_id = knowledge_source.current_version_id`,
  so semantic search can never reach a superseded passage.
* **Facts kept history and could not distinguish it.** `value` rows are
  written with `ON CONFLICT DO NOTHING` on `(entity_id, attribute_id, value)`.
  When a price changes from $500 to $800, **both rows survive**, under the
  same entity and attribute, with no timestamp and no version link. Structured
  lookup returned both, and nothing marked which was current.

The second is the more serious: a missing answer is visibly missing, whereas
two contradictory prices presented as equally true is a *wrong* answer
delivered confidently.

**The decision taken: keep history and mark it.** The rate that used to apply
is a real fact, and deleting it makes the original question permanently
unanswerable. Retrieval filters to current values, so the contradiction stops;
the history accumulates for a temporal feature that can be built later without
a migration. **The chunk half is deliberately unchanged** — semantic search
still sees only current versions, so no answer can cite content that has been
replaced.

### What the live data actually showed

Worth recording, because it is not what this item predicted. There were **48
entity+attribute pairs holding several values** — but `STALE` versions
numbered **zero**, so none of them were superseded content. They were three
different things wearing the same shape:

| Pair | Values | What it is |
|---|---|---|
| `Alpinist Studios / client_industries` | E-commerce, Fintech, Health Tech, Enterprise | genuinely multi-valued — all true at once |
| `MVP / definition` | five paraphrases of one sentence | extraction noise |
| *a changed price* | — | zero instances |

And `Attribute.multivalue` — the column that exists precisely to tell the
first row from the third — was **hardcoded `False`** in
`extraction/agent.py`, so every attribute in the database claimed to be
single-valued while dozens were not.

### The design

**Provenance is a link table, not a column.** A `value` row is global — unique
on (entity, attribute, value) — because the same fact is often stated by
several documents, and storing it once is what keeps "Alpinist Studios employs
Justin Flores" from appearing five times. A single `version_id` column could
not express that, and without it supersession is not *decidable*: a fact
dropped by one document may still be asserted by another.

**Currency is derived, not accumulated.** `resolve_superseded_values` recomputes
the whole state from provenance on every run: a value is current when at least
one version asserting it is its source's `current_version_id`, superseded when
none are. Nothing is incremental, so a re-ingest, a rollback, or a document
that stops being current all converge on the same answer without anyone
reasoning about the order they happened in.

Four decisions that each prevent a bug:

* **Facts that come back are un-marked.** A value removed in v2 and restated
  in v3 is current again; a one-way mark would leave it permanently invisible.
* **The pass runs after cutover, not before** — `current_version_id` is what
  "current" means, and the cutover is what sets it. Running it first would
  mark the incoming version's own facts as superseded.
* **A failed currency pass does not fail the job.** The facts are written and
  the document is indexed; the pass is derived from scratch each time, so the
  next ingestion of any document repairs it.
* **Values with no provenance are never touched.** Every row predating this
  change is in that state, which is why the migration needs no backfill —
  which version asserted an existing fact is not recoverable, so they stay
  current, exactly as they were.

**`multivalue` is now observed rather than asked for.** If a document states
four `client_industries` for one entity, the attribute takes more than one
value — a fact about the data, not a judgement call. Counted across the whole
document (a document's four industries are usually spread over four chunks, so
per-chunk counting would see one each) and per entity (fifteen people with one
role each does not make `role` multi-valued — verified live: 15 people, 15
roles, still `multivalue=false`). Distinct values only, so the same fact
restated in two chunks is one value.

Retrieval also gained an **ORDER BY**. Neither value query had one, so several
current values came back in scan order and the caller read them as a list of
equally-weighted facts; newest-first is at least defensible, and arbitrary is
not.

**What this does not fix:** the paraphrase noise above (`MVP / definition` ×5)
is an extraction-quality problem, not a currency one — five near-identical
sentences are five distinct `value` rows because the unique constraint is on
exact text, and no amount of supersession logic will merge them.

**Verified on live Postgres**, not only SQLite — the correlated `EXISTS`
subqueries are what SQLite cannot prove. A throwaway source with two versions:
with v1 current, `$500` stood and `$800` was superseded; cutting over to v2
swapped them (1 superseded, 1 restored); a re-run changed nothing `(0, 0)`;
rolling back to v1 swapped them again. Then a real document was re-ingested —
15 provenance rows recorded, **0 values wrongly superseded**, corpus unchanged
at 777 values across 24 indexed sources. Migration `d5a91c3f7b28`; 14 tests in
`tests/ingestion/test_fact_supersession.py` and 3 in
`test_structured_lookup.py`. Suite 897 → **911 passing**.

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

**The retry half shipped 2026-09-10**, with item 7 — see below.

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

**7. Cap attempts and dead-letter the rest.** ✅ **Done 2026-09-10**, together
with the retry half of item 6 and item 13.

Every ingestion failure used to be terminal: `complete_job` wrote `FAILED` and
that was the end of the document, whatever the reason. That was worst for
exactly the most common failure — provider throttling — where waiting ten
minutes is the entire fix. With the deterministic failures gone (items 8 and
8b), *everything still failing in this database was transient*: the last two
documents died on a daily token ceiling that a retry an hour later simply
walks past.

**The policy is a pure function.** `ingestion/queue/retry.py` decides on
`(failure_kind, attempt_count)` and nothing else — no I/O, no clock beyond
`now` — so "does this failure deserve another go?" is answerable in a unit
test without a database or a provider. The queue layer owns the write; the
policy owns the decision.

Three outcomes, and the third is the point of this item:

| Outcome | When | What it tells a human |
|---|---|---|
| `QUEUED` + `next_attempt_at` | transient, budget left | nothing — it will fix itself |
| `DEAD_LETTER` | transient, budget spent | it kept failing for a reason that usually passes |
| `FAILED` | terminal | it cannot work as it stands |

`FAILED` and `DEAD_LETTER` both need a human but need *different things* from
one, which is why collapsing them was the gap.

**Decisions worth recording.**

* **Terminal by default.** Only five kinds are transient
  (`tika_transient`, `eav_extraction_rate_limited`, `eav_extraction_timeout`,
  `storage_fetch_failed`, `worker_abandoned`). A failure kind nobody has
  classified yet stops after one attempt and stays visible, rather than
  quietly spending a full retry budget on every job that hits it.
* **429 got its own `Err` code.** Throttling and refusal used to arrive at
  the queue as the same `eav_extraction_failed`, distinguishable only by
  re-parsing the message two layers away. `_stage_eav_extraction` now splits
  them where the exception is still in hand, using the same
  `is_rate_limited` predicate the extraction agent's own backoff uses — one
  definition, two callers, no drift.
* **Backoff starts at 10 minutes, caps at 60.** Chosen against what is being
  waited for: a per-minute token bucket refills many times over in ten
  minutes, and the cap keeps the last attempt inside the same working day, so
  a document that fails in the morning is not first retried after midnight.
* **Four attempts.** Every retry re-runs the *whole* pipeline — fetch, Tika,
  chunk, embed, extract — so an attempt is expensive, and against a daily
  quota a job retrying forever spends the budget that the jobs which would
  succeed need.
* **A worker death consumes an attempt.** The stale-job reaper now counts its
  requeue. Without that, a document that kills the worker every time — an OOM
  on a huge PDF — is requeued forever, and because the reaper runs at startup
  it gets a fresh worker to kill each time. Now it dead-letters like any other
  repeat offender.
* **A requeued job clears `completed_at`.** A pending retry has not completed,
  and a stale timestamp would sort it in among the finished work on the Jobs
  page.
* **The claim query honours the backoff.** Without
  `next_attempt_at IS NULL OR next_attempt_at <= now()`, "retry in ten
  minutes" would mean "retry on the next poll" — a busy-wait against the very
  thing that was throttling us. `NULL` means ready now, which is why the rows
  already in the database needed no backfill.

**Verified against live Postgres**, not only SQLite — the enum value and the
claim SQL are the parts SQLite cannot prove. A temporary job row was held back
by its backoff (not claimed), became claimable once it elapsed, was requeued
on a simulated 429 with `completed_at` left null, reached `DEAD_LETTER` when
its budget ran out, and a `checksum_mismatch` on a fresh row went straight to
`FAILED` after one attempt. The row was removed afterwards.

22 tests in `tests/ingestion/test_job_retry.py`; suite 853 → **875 passing**.

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

**12. Consider concurrent jobs.** ✅ **Done 2026-09-10.**

The worker was strictly serial: claim one, finish, claim the next. It now runs
`PGQUEUE_CONCURRENCY` lanes (default **2**) — several claim-process-record
loops in **one process**, not several processes. That distinction is what made
this affordable: a second process loads its own 400 MB copy of the embedding
model, which was the stated blocker, and lanes share one copy.

**What it buys, stated honestly.** Extraction dominates a document's wall
clock and is bound by a provider token budget, not by this worker — so two
lanes do not double throughput against a daily ceiling. What they do is stop
one slow document holding the queue head: while a lane waits on the model,
another fetches, runs Tika and embeds. A document that fails early no longer
makes everything behind it wait. Two rather than more, because past that the
extra lanes mostly generate 429s, and spending quota on backoff is not
throughput.

**Two hazards, both real, both closed.**

*The shared embedding model.* This item already warned that a
`SentenceTransformer` is not safe to encode from several threads at once, and
`chunk_and_embed` runs `process_document` under `to_thread` with a shared
tokenizer and model — so lanes would have done exactly that. An
`asyncio.Lock` around the call serialises encoding, which costs almost nothing
because embedding is a small fraction of a document's time.

*A race the doc did not anticipate.* This item states that `FOR UPDATE SKIP
LOCKED` and the one-job-per-source guard already make concurrent claiming
safe. The first is true; **the second is not, and the two are not the same
question.** `SKIP LOCKED` stops two lanes taking the same *row*. The guard
asks "does this source already have a RUNNING job?" — and a lane's RUNNING
transition is invisible to the others until it commits, so two lanes selecting
at the same instant can each pick a *different* job for the *same* source and
both proceed, which is precisely what the guard exists to prevent. Claims are
now serialised with a lock held only for the claim; it is fast, so this costs
nothing measurable. The lock lives on `WorkerDeps` rather than at module
scope, because an `asyncio.Lock` binds to the loop that first acquires it and
a process-global one outlives the loop it was bound to.

That window remains, pre-existing, between two worker *processes*.
`PGQueueSettings.visibility_lock_id_namespace` is the hook for closing it with
a Postgres advisory lock if a deployment ever runs more than one.

**Verified live.** Two documents claimed 187 ms apart and extracting
simultaneously:

```
04:18:12.466 [1c66bbf2.1] claimed job ... attempt 1
04:18:12.653 [3754e16c.1] claimed job ... attempt 1
04:18:15.426 [1c66bbf2.1] Extracting Contact: 1 chunk(s), 1 window(s)...
04:18:15.740 [3754e16c.1] Extracting Contact: 1 chunk(s), 1 window(s)...
```

Three jobs, all `SUCCEEDED`, corpus unchanged. Note that both documents are
named `Contact` — without item 14's trace ids those four lines would be
unreadable, which is why that item went first. 5 tests in
`tests/ingestion/test_worker_loop.py`.

*Items 10 and 11 done 2026-09-09; item 12 done 2026-09-10.*

### P3 — observability and hygiene

**13. Structure the failure reason.** ✅ **Done 2026-09-10**, with items 6 and
7 — the retry policy has to dispatch on the failure kind, and doing that by
splitting `error_details` on its first colon is exactly the fragility this
item was about. `failure_kind` is a real column now, backfilled from the
existing `error_details` so the 49 failures already in the database became
groupable immediately rather than only newly-failing ones. `attempt_count`,
`next_attempt_at` and `failure_kind` are all served by `GET /admin/jobs` and
shown on the Jobs page, which gained an **Attempts** column that marks a
pending retry — status alone cannot say whether a `QUEUED` row is fresh work
or a job waiting out its backoff.

**14. Emit the `trace_id`.** ✅ **Done 2026-09-10.**

Every log line now carries the run that produced it:

```
2026-09-10 04:18:15,426 INFO [1c66bbf2.1] Extracting Contact: 1 chunk(s)...
2026-09-10 04:18:02,192 INFO [-] ingestion worker starting: 2 lane(s)...
```

**The id names the *run*, not the job.** `job_id` alone stopped being enough
the moment item 6 shipped: a job can be retried four times, so one id would
label four separate runs. `<job8>.<attempt>` separates them while keeping the
job's prefix, so one grep still finds every attempt of a document.

**A ContextVar, not a parameter.** The lines that need labelling are emitted
deep inside modules with no reason to know about jobs — the extraction agent
warning about a dropped window, `persistence` about a missing chunk. Threading
an id through all of them would be a far worse trade than reading it from the
ambient context. ContextVars are copied per asyncio task *and* propagated by
`asyncio.to_thread`, so a line logged from the extraction thread carries the
id of the coroutine that started it — which matters, because extraction is the
part that runs in a thread.

The filter is attached to the **handler**, not a logger: a filter on a logger
is not applied to records that reach the root handler by propagation, and
propagation is how nearly every line in this codebase is emitted. It must also
be installed before the first line is logged, because a format naming
`trace_id` raises inside `logging` on a record that lacks the attribute — an
unlabelled record gets `-` rather than an exception.

This became a prerequisite rather than a nicety once item 12 landed: two
concurrent lanes interleave into one stream, and the live run below happened
to process two different documents both named `Contact`. Without the id those
lines are indistinguishable. 5 tests in `test_worker_loop.py`.

**15. Test the pipeline core.** ✅ **Done 2026-09-10.** `persistence.py` got
its tests with the P0 work (`test_chunk_persistence.py`); the worker loop was
the half still missing. `poll_once` is where a claimed job, a handler and the
retry policy meet, and the adjacent files covered everything around it —
the reaper, the pipeline's rollback — but not the thing that calls them.

22 tests in `tests/ingestion/test_worker_loop.py`, backed by real SQLite rows
and fake handlers so the claim query and the status writes are exercised while
Tika, MinIO and the provider stay out of it. What they pin: dispatch by
`job_type` (a table lookup, so a `DELETE` job can never run the ingestion
pipeline), one job per tick, the crash path, the four statuses the retry work
made possible, startup reaping, the poll-interval sleep, and the
one-job-per-source guard — which had no test at all despite being what makes
`FOR UPDATE SKIP LOCKED` safe to point at more than one worker.

**It immediately found a defect in item 7, one day old.** The retry work gave
every transient failure a ten-minute backoff, and `worker_abandoned` is
transient — so an ordinary deploy silently cost *every in-flight document* ten
idle minutes before it resumed. The reaper runs at worker startup, and a
stale-but-reaped job was no longer claimable on the tick that reaped it.

The fix is a distinction the policy was missing. A backoff answers *"the
condition that caused this needs time to clear"* — a token bucket refilling, a
Tika coming back up. A worker that died has already cleared by definition: the
process doing the reaping is its replacement. `retry.IMMEDIATE_KINDS` now
names the kinds that go back on the queue with no delay. The attempt cap still
applies to them, which is what actually protects against a document that kills
every worker that touches it — the backoff never did.

That is the argument for this item in one example: the behaviour was wrong in
a way no unit test of `retry.decide` would have shown, because both halves
were individually correct and only their composition was not.

Suite 875 → **897 passing**.

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

*Items 5, 6, 7, 12, 13, 14 and 15 shipped 2026-09-10.*

**Every item in this document is now closed.** 16 (`reconcile.py`) was
already effectively resolved — it has a `__main__` entry point and
`tests/ingestion/test_reconcile.py` — and 17 (stale docstrings) was cleaned up
with item 10.

What is left is not on this list, because it was found while working through
it:

1. **Extraction produces paraphrase duplicates.** `MVP / definition` holds five
   near-identical sentences, and `Agile / flexibility` holds "high", "True"
   and "offers flexibility" twice. They are distinct `value` rows because the
   unique constraint is on exact text. This is an extraction-quality problem —
   supersession will not merge them, and item 5 deliberately did not try.
2. **The crawler ingests error pages.** One of the two `Contact` sources in
   this corpus is a 404 page whose text begins "# Oops!". It was chunked,
   embedded and indexed like any other document, and is now retrievable.
3. **A worker advisory lock**, if a deployment ever runs more than one worker
   process — see item 12.

A reasonable check that it worked: re-ingest the 24 existing sources and
expect a failure rate in the low single digits, with any remaining failures
carrying a `failure_kind` that says something more useful than
`persist_failed`.

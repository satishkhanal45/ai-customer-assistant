# Crawler — Frontend Integration Guide

How the web frontend integrates with the AI Customer Assistant's document
crawler/ingestion API. This covers every endpoint you need, the request/response
shapes, and the recommended frontend flows.

---

## 1. API base URL

There is **no `/api` prefix**; the routers are mounted at the app root.

| Environment | Base URL |
| --- | --- |
| Same-origin (frontend served by the FastAPI app) | `location.origin` (recommended) |
| Dev / `file://` | `http://127.0.0.1:8000` |
| Docker | `http://<host>:{APP_PORT:-8000}` (host port defaults to `8000`) |

The frontend already resolves this in `frontend/src/config.js` via
`NS.config.apiBase`. Use it for every call below.

> CORS is wide open (`allow_origins=["*"]`) for dev. There is currently **no
> auth** on the ingest routes.

---

## 2. Endpoints at a glance

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/ingest/crawl` | Crawl **one page** (`scope: "PAGE"`) or run **discovery** for review (`scope: "SITE"`). |
| `POST` | `/ingest/crawl/discover` | Sitemap-first discovery, returns a cached review list (SITE flow). |
| `POST` | `/ingest/crawl/{discovery_id}/confirm` | Crawl + ingest the items from a previous discovery. |
| `GET` | `/ingest/jobs/{job_id}` | Poll the status of a single ingestion job. |
| `POST` | `/ingest/upload` | Upload a PDF / DOCX / Markdown file. |
| `GET` | `/health` | Liveness check (`{"status": "ok"}`). |

---

## 3. The two crawl models

The crawler has two distinct frontend-facing models:

1. **Single page (PAGE)** — you give one URL, it is fetched and ingested
   immediately. No review step.
2. **Site (SITE)** — discovery-first. `POST /ingest/crawl` with
   `scope: "SITE"` (or `/ingest/crawl/discover`) returns a **review list**
   cached server-side. The UI shows it to the user, then
   `POST /ingest/crawl/{discovery_id}/confirm` actually crawls and ingests the
   confirmed pages.

Discovery results are cached **server-side for 600 seconds** (10 minutes). After
that a confirm request returns `404` and the UI must re-run discovery.

### Wait strategy (SPA / client-rendered sites)

Both `POST /ingest/crawl` and `POST /ingest/crawl/discover` accept:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `wait_strategy` | `"fixed_timeout" \| "networkidle" \| "selector"` | `"fixed_timeout"` | How long to wait for client-rendered content before capturing HTML. |
| `wait_selector` | `string \| null` | `null` | CSS selector to wait for. **Required** when `wait_strategy == "selector"`. |

- `fixed_timeout` — captures right after the page `load` event. Fine for plain
  sites; **captures the loading shell on SPAs** (e.g. just `"Loading"`).
- `networkidle` — waits for the network to go quiet (covers delayed `fetch`).
- `selector` — waits for a specific element to appear before capturing.

If `wait_strategy` is `"selector"` without `wait_selector`, the API returns
`422`.

---

## 4. Endpoint reference

### 4.1 `POST /ingest/crawl`

Crawl a single URL, or run discovery for a site.

**Request body** (`application/json`):

```jsonc
{
  "url": "https://example.com/page",   // required, 5..2048 chars
  "scope": "PAGE",                     // "PAGE" | "SITE", default "PAGE"
  "category_id": null,                 // optional knowledge category UUID
  "wait_strategy": "fixed_timeout",    // optional
  "wait_selector": null                // required only for "selector"
}
```

**When `scope: "PAGE"`**

If the URL is an HTML page, it is fetched + ingested and returns a single
submission record:

```jsonc
// 200 OK
{
  "status": "submitted",           // or "duplicate_skipped" (checksum dedup)
  "job_id": "9c3b…-uuid",
  "source_id": "…-uuid",
  "version_id": "…-uuid"
}
```

If the URL is a document (PDF/DOCX) it returns the bulk shape below
(`results` array with one entry).

Errors: `400` — URL could not be fetched; `502` — page fetched but HTML
extraction failed.

**When `scope: "SITE"`**

Runs discovery and returns a **review payload** (no crawling yet). The
`discovery_id` is used later for confirm:

```jsonc
// 200 OK
{
  "discovery_id": "a1b2c3…",        // hex, valid ~600s server-side
  "source": "SITEMAP",              // "SITEMAP" | "BFS_FALLBACK"
  "page_count": 12,
  "document_count": 1,
  "pages": [
    { "url": "https://example.com/",   "kind": "PAGE",     "file_type": null },
    { "url": "https://example.com/a.pdf", "kind": "DOCUMENT", "file_type": "PDF" }
  ]
}
```

`kind` is `"PAGE"` (crawl for HTML) or `"DOCUMENT"` (download raw bytes).

---

### 4.2 `POST /ingest/crawl/discover`

The same discovery step as `scope: "SITE"` above, as a standalone endpoint.

**Request body:**

```jsonc
{
  "root_url": "https://example.com",   // required
  "wait_strategy": "fixed_timeout",
  "wait_selector": null
}
```

**Response:** identical review payload as `scope: "SITE"` (4.1).

Use this when the "submit" and "review" steps are separate UI screens.

---

### 4.3 `POST /ingest/crawl/{discovery_id}/confirm`

Crawl and ingest the pages from an earlier discovery.

**Request body** (optional `category_id`):

```jsonc
{ "category_id": null }
```

**Response — 200 OK:**

```jsonc
{
  "status": "submitted",
  "results": [
    {
      "url": "https://example.com/",
      "status": "submitted",          // per-item; "duplicate_skipped" possible
      "job_id": "…-uuid",
      "source_id": "…-uuid",
      "version_id": "…-uuid"
    }
    // or { "url": "…", "status": "failed", "error": "reason" } on crawl failure
  ]
}
```

**404** — `{"detail": "Unknown or expired discovery_id. Run /crawl/discover again."}`
(the 600s cache expired or the id was never created). The UI must re-run
discovery in this case.

---

### 4.4 `GET /ingest/jobs/{job_id}`

Poll the status of a single ingestion job. **This is how the frontend knows an
ingestion finished.**

**Response — 200 OK:**

```jsonc
{
  "job_id": "…-uuid",
  "status": "QUEUED",        // "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED"
  "chunks_created_count": 0,
  "entities_created_count": 0,
  "error_details": null      // string when FAILED
}
```

**404** — `{"detail": "Job not found"}` (unknown job id).

> Jobs run **asynchronously** (a background task). Poll until `status` is a
> terminal value (`SUCCEEDED` or `FAILED`).

---

### 4.5 `POST /ingest/upload`

Upload a file for ingestion.

**Form-data:**
- `file` — the file (PDF, DOCX, or Markdown).
- `category_id` (optional) — knowledge category UUID.

**Response — 200 OK** (same as a single-page submit):

```jsonc
{
  "status": "submitted",           // or "duplicate_skipped"
  "job_id": "…-uuid",
  "source_id": "…-uuid",
  "version_id": "…-uuid"
}
```

Errors: `415` — unsupported media type; `400` — empty file.

---

## 5. Frontend implementation

### 5.1 Single-page crawl (PAGE)

```js
const res = await fetch(`${apiBase}/ingest/crawl`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    url,
    scope: 'PAGE',
    wait_strategy: 'networkidle',   // use networkidle/selector for SPAs
  }),
});
const body = await res.json();
if (!res.ok) { /* show body.detail */ }

if (body.status === 'submitted') {
  await pollJob(body.job_id);       // see 5.4
}
```

### 5.2 Site flow (discover → review → confirm)

```js
// 1) Discover
const disc = await fetch(`${apiBase}/ingest/crawl/discover`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ root_url: siteUrl, wait_strategy: 'networkidle' }),
}).then(r => r.json());
// disc = { discovery_id, source, page_count, document_count, pages }

// 2) Show review list to the user, then confirm their selection
const conf = await fetch(`${apiBase}/ingest/crawl/${disc.discovery_id}/confirm`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ category_id }),
}).then(r => r.json());

// 3) Poll each submitted job
if (conf.status === 'submitted') {
  for (const item of conf.results) {
    if (item.status === 'submitted') await pollJob(item.job_id);
  }
}
```

> On a `404` from confirm, re-run discovery (the 600s cache expired).

### 5.3 Upload

```js
const fd = new FormData();
fd.append('file', fileInput.files[0]);
if (categoryId) fd.append('category_id', categoryId);

const res = await fetch(`${apiBase}/ingest/upload`, { method: 'POST', body: fd });
const body = await res.json();
if (body.status === 'submitted') await pollJob(body.job_id);
```

### 5.4 Job polling

```js
const TERMINAL = new Set(['SUCCEEDED', 'FAILED']);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pollJob(jobId, onUpdate) {
  for (let i = 0; i < 120; i++) {          // ~2 min cap
    const res = await fetch(`${apiBase}/ingest/jobs/${jobId}`);
    if (res.status === 404) return null;   // job gone
    const job = await res.json();
    onUpdate?.(job);
    if (TERMINAL.has(job.status)) return job;
    await sleep(1000);
  }
  return null;                              // still running after cap
}
```

### 5.5 Suggested UI state machine

```
idle
 └─ submit(PAGE) ───────────────> polling(job) ──SUCCEEDED──> done
                                  └──FAILED──> error

site: idle
 └─ discover ─> review(pages shown)
      └─ confirm ─> polling(each job) ─> done / error
      └─ (cache expires) ─> re-run discover
```

Use the `pages` list (`kind`, `file_type`) from the review payload to render a
checklist. Gray out the confirm button if `page_count + document_count === 0`.
Surface `error_details` from a failed job to the user.

---

## 6. Error-handling checklist

- `404` on `/ingest/jobs/{id}` — job unknown; treat as gone.
- `404` on `/ingest/crawl/{id}/confirm` — discovery expired; re-run discovery.
- `422` — validation error (e.g. `wait_strategy: "selector"` without
  `wait_selector`).
- `400` on `POST /ingest/crawl` — URL fetch failed.
- `415` on `POST /ingest/upload` — unsupported file type.
- Per-item `results[].status === "failed"` carries an `error` string.

All error bodies use FastAPI's `{"detail": "..."}` shape.

---

## 7. Notes / limitations

- **Asynchronous ingestion.** The API returns `job_id`s immediately; content is
  ready only once a job reaches `SUCCEEDED`.
- **Discovery cache TTL:** 600 seconds. Confirm must follow discover promptly.
- **No pagination/listing endpoint** for sources is registered yet (the
  `/admin/knowledge-sources` API is not wired up). Only job status is
  queryable today.
- **Wait strategy default is `fixed_timeout`**, which will capture a loading
  shell on SPA sites. Prefer `networkidle` or `selector` when crawling
  client-rendered sites.
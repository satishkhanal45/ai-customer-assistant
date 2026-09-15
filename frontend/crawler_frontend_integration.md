# Crawler — Frontend Integration Guide

> **Current.** Every endpoint documented here was verified against
> `api/ingest.py` on 2026-09-08. Updated for P0-3: **every ingest route now
> requires an authenticated `member`**, and the crawler refuses URLs that do
> not resolve to a publicly-routable address. See §1.1 and §6.

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

---

## 1.1 Authentication (P0-3)

**Every route in this guide requires a signed-in `member`.** An
unauthenticated call gets `401` with a `WWW-Authenticate` header.

In the browser there is nothing to do beyond sending cookies: the session is
a pair of `HttpOnly` cookies the browser attaches itself, and no token is
readable from JavaScript. Two consequences for the code in §5:

1. **Every `fetch` needs `credentials: 'include'`.** Without it the browser
   omits the cookies and the call 401s. The snippets below include it.
2. **Prefer `NS.api` over raw `fetch`.** `frontend/src/api.js` already sends
   credentials, and — more importantly — it refreshes and replays once on a
   `401`, through a *single shared* in-flight refresh. Access tokens last
   fifteen minutes, so expiry mid-session is ordinary rather than
   exceptional. Rolling your own refresh per call is actively dangerous:
   refresh tokens rotate, so two concurrent refreshes present the same token
   twice, which the server correctly reads as theft and answers by revoking
   every session the user has.

From a script, log in with `transport: "bearer"` and send
`Authorization: Bearer <token>`. The root `README.md` has a worked example.

CORS is no longer open. It is off entirely by default, which restricts
nothing: the frontend is served from the API's own origin, so its requests
are same-origin and never consult CORS. `CORS_ALLOW_ORIGINS` is an explicit
allowlist for a genuinely cross-origin frontend; a wildcard is not accepted,
because the session is a cookie.

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

Errors: `400` — URL could not be fetched, **or refused by the SSRF guard**
(the `detail` says why: a private, loopback, link-local or otherwise
non-routable address, a disallowed scheme, or a redirect to one of those);
`502` — page fetched but HTML
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
  credentials: 'include',            // send the session cookies (§1.1)
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
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ root_url: siteUrl, wait_strategy: 'networkidle' }),
}).then(r => r.json());
// disc = { discovery_id, source, page_count, document_count, pages }

// 2) Show review list to the user, then confirm their selection
const conf = await fetch(`${apiBase}/ingest/crawl/${disc.discovery_id}/confirm`, {
  method: 'POST',
  credentials: 'include',
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

const res = await fetch(`${apiBase}/ingest/upload`, {
  method: 'POST',
  credentials: 'include',
  body: fd,
});
const body = await res.json();
if (body.status === 'submitted') await pollJob(body.job_id);
```

### 5.4 Job polling

```js
const TERMINAL = new Set(['SUCCEEDED', 'FAILED']);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pollJob(jobId, onUpdate) {
  for (let i = 0; i < 120; i++) {          // ~2 min cap
    const res = await fetch(`${apiBase}/ingest/jobs/${jobId}`, {
      credentials: 'include',
    });
    if (res.status === 404) return null;   // job gone
    if (res.status === 401) return null;   // signed out mid-poll; NS.api
                                           // handles this properly
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

- `401` on any route — not signed in, or the access token expired. If you
  are using `NS.api` this is already handled: it refreshes once and replays.
  On a second `401`, send the user to the login page.
- `403` — signed in, but not permitted. Refreshing will **not** help, so do
  not retry; show the message.
- `429` — rate limited (10 ingest calls a minute per user). Honour the
  `Retry-After` header rather than retrying immediately.
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
- **The crawler will not fetch internal hosts.** Since P0-3 every URL — the
  one you submit, every redirect hop, and every link followed during a site
  crawl — must resolve to a publicly-routable address. `localhost`,
  `127.0.0.1`, `10.x`, `192.168.x`, `169.254.169.254` and `file://` are all
  refused with `400`. To crawl an intranet site deliberately, name its
  network in `CRAWL_ALLOW_ADDRESSES` (a CIDR list, so exempting one range
  does not exempt the cloud metadata endpoint).
- **Uploads are attributed to the signed-in user.** `knowledge_source.uploaded_by`
  is the caller's account, not the service account. Rows ingested before
  P0-3 still point at the service account; that history cannot be
  reconstructed.
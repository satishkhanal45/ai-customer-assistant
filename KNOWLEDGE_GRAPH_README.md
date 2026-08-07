# Knowledge Graph — What's Been Done

The crawled website content is ingested into an EAV (Entity–Attribute–Value)
schema, and a read-only HTTP API exposes it as a queryable graph. A Phase 3
frontend viewer renders it with Cytoscape.js.

## Components

| Layer | Location | Purpose |
|---|---|---|
| EAV tables | Postgres (`pgvector`) | `knowledge_entity`, `knowledge_attribute`, `knowledge_value`, `knowledge_relation` |
| Traversal layer | `backend/src/ai_customer_assistant/ingestion/graph/queries.py` | read-only SQL over EAV: `get_entity`, `find_entities`, `get_neighbors`, `find_path` (recursive CTE, undirected edges) |
| HTTP API | `backend/src/ai_customer_assistant/api/graph.py` | FastAPI router, prefix `/graph`, thin shape-conversion over `queries.py` |
| App entry | `backend/src/ai_customer_assistant/main.py` | FastAPI `app` mounts the graph router + CORS (all origins, for the `file://` viewer) |
| Viewer | `frontend/graph_viewer.html` | single-file Cytoscape.js app: search, click-for-attributes, double-click-to-expand, expand-all, path finder, color-by-type legend, 3 layouts |

## How to run

Prereqs: `docker compose` running `postgres`, `minio`, `tika`, and the EAV
data already ingested (see `EAV_INGESTION_FIX_README.md` / `EAV_DATA_README.md`).

### 1. Start the API

**Option A — docker** (applies the entrypoint fix, stops the crash-loop):

```bash
docker compose up -d --build backend
```

**Option B — local uvicorn** (what is currently running):

```bash
set -a; source backend/.env; set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
PYTHONPATH=backend/src/ai_customer_assistant \
uv run --project backend uvicorn main:app \
  --app-dir backend/src/ai_customer_assistant \
  --host 0.0.0.0 --port 8000
```

Note the `--app-dir`: without it, `uvicorn main:app` resolves the placeholder
`backend/main.py` (which has no `app`) and the server exits with
`Attribute "app" not found in module "main"`.

### 2. Verify the API

```bash
curl http://127.0.0.1:8000/health
curl "http://127.0.0.1:8000/graph/search?q=alpinist&limit=5"
```

### 3. Open the viewer

```bash
open frontend/graph_viewer.html
```

It is served straight from disk (`file://`); CORS is already enabled on the API.
The viewer calls `http://127.0.0.1:8000/graph` — if you run the API on a
different port, change the `API` constant at the top of the `<script>` block in
`frontend/graph_viewer.html`.

## API reference (all GET, read-only)

| Endpoint | Response |
|---|---|
| `/graph/search?q=&entity_type=&limit=` | list of `{id, entity_type, name, label}` |
| `/graph/entities/{id}` | `{entity, facts[]}` — entity + attributes (`namespace`, `attribute_name`, `value`, `value_type`, `multivalue`, `searchable`) |
| `/graph/entities/{id}/neighbors?depth=` | `{nodes[], edges[]}` — subgraph (edges are undirected) |
| `/graph/subgraph?entity_id=&depth=` | alias of neighbors |
| `/graph/path?source=&target=&max_depth=` | ordered `{id, entity_type, name, label}[]` path, or `null` if none |

## Verified end-to-end

- 2-hop path: `Ashok Maharjan → Alpinist Studios → Atishaya Maharjan`
- Neighborhoods return typed edges (e.g. `advisory board member`, `employs`)
- All 6 target URLs crawl + extract EAV; `/about/` alone produced
  29 entities / 5 attributes / 7 values / 18 relations / 17 entity-map rows
- CORS headers confirmed (`access-control-allow-origin: *`)

## Known issues / limitations

- **Double processing** — running `make ingest` and the background worker
  (`scripts/run_worker.py`) against the same source concurrently causes
  duplicate `embedding_chunk` rows and `persist_failed: Multiple rows were
  found when exactly one was required`. Run one or the other, or only re-run
  already-ingested sources via a clean REINDEX.
- **Flaky tool calls** — `llama-3.3-70b-versatile` occasionally emits a
  malformed EAV tool call (e.g. `no_fact_found`), which Groq rejects with
  `tool_use_failed` (HTTP 400). This is model-side and non-deterministic; a
  failed chunk/job does not affect already-persisted data.
- **Near-duplicate entities** — name resolution is inconsistent across chunks,
  so e.g. `Alpinist`, `Alpinist Studios`, `organization: Alpinist Studios` all
  exist as separate nodes. Dedup/merge is not yet implemented.
- **Re-running stale jobs** — sources already ingested earlier left job rows in
  `QUEUED`/`RUNNING`; re-claiming them now fails with duplicate-row errors
  (expected, not a code regression).

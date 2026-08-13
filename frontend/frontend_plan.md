# Frontend Implementation Plan — AI Customer Assistant

Status: **Phases 0–3 implemented. Phase 4 UI implemented (backend-blocked — endpoints not wired). Phase 5 partial (responsive CSS + basic polish).**
Scope: `frontend/` — build the missing customer portal (chat), ingestion UI, admin dashboard, and unify/upgrade the existing knowledge-graph explorers around the ready backend.

---

## 1. Current state

### Backend (complete, all APIs live)

| API | Method | Contract | Status |
|---|---|---|---|
| `/chat` | POST | `{thread_id, message}` → `{thread_id, reply, trace_id}` | Registered (`api/routes.py`) |
| `/graph/search` | GET | `?q=&entity_type=&limit=` → `list[EntityRef]` | Registered |
| `/graph/search_value` | GET | `?q=&limit=` → `list[EntityRef]` | Registered |
| `/graph/entities/{id}` | GET | → `EntityDetail` (entity + facts) | Registered |
| `/graph/entities/{id}/neighbors` | GET | `?depth=` → `GraphFragment {nodes, edges}` | Registered |
| `/graph/subgraph` | GET | `?entity_id=&depth=` → `GraphFragment` | Registered |
| `/graph/path` | GET | `?source=&target=&max_depth=` → `list[EntityRef]` | Registered |
| `/ingest/upload` | POST | multipart file (PDF/DOCX/MD) → job/source/version ids | Registered |
| `/ingest/crawl` | POST | `{url, category_id}` → job/source/version ids | Registered |
| `/health` | GET | → `{status: ok}` | Registered |
| `/admin/knowledge-sources` | POST | upload via storage pipeline | **NOT registered** — placeholder deps raise `NotImplementedError` (`storage/api.py:62-83`) |

Notes:
- Chat history is **server-side** (LangGraph checkpointer keyed by `thread_id`). The client only ever sends the new `message` + `thread_id`. Multi-turn `interrupt()` flows (ticket email collection, escalation confirmation) resume automatically on the next message — no client special-casing required beyond "keep sending the same `thread_id`".
- `api/chat.py` defines an *alternate* contract (`{message, history}` → `{answer, sources, matches}`) that is **not registered** and **does not compile** — it imports `services.chat_service.answer`, which does not exist (only `handle_message`/`build_chat_service`). Dead/broken code; never target it.
- `auth/jwt.py` is an **empty placeholder (0 lines)** — no JWT implementation, no auth middleware registered.

### Frontend (2 static HTML files, nothing else)

- `graph_viewer.html` — 2D cytoscape graph browser (search, seed/expand, path A/B, legend, detail panel).
- `graph_viewer_3d.html` — unified 2D/3D force-graph explorer (Neural Graph) with the same features + HUD + auto-rotate.
- Both hardcode `http://127.0.0.1:8000/graph`, load engines from `unpkg.com`, and are opened as local files (Makefile `graph` target). The `frontend/` dir is volume-mounted into the container (`docker-compose.yml:95`) but **not actually served** by the app — see 6.1.

### Critical gaps

1. **No chat UI** — the entire multi-agent conversational backend exists with no interface.
2. **No ingestion UI** — upload/crawl endpoints exist but nothing calls them.
3. **No admin/dashboard UI** — no source list, job status, or stats view.
4. **No app shell / navigation** — two disconnected standalone HTML pages.
5. **~80% duplicated logic** between the two graph pages.

---

## 2. Stack decision

**Recommendation: keep it vanilla, modular ES modules — no build step.**

Rationale:
- The backend already serves the frontend via docker volume mount; no static bundler in the pipeline.
- Existing pages are vanilla JS + CDN libs; a framework would be inconsistent with the current approach.
- Vanilla keeps the app deployable exactly like today (`docker compose up`, open the page).

Fallback (if product requirements grow): React + Vite, served as a built artifact mounted into the backend image. Decision can be revisited at Phase 3+ without rework if the API client and page structure are kept framework-agnostic.

Proposed structure:

```
frontend/
├── frontend_plan.md            # this document
├── index.html                  # app shell: nav (Chat / Graph / Ingest / Admin)
├── src/
│   ├── api.js                  # shared fetch client (base URL config, errors, timeouts)
│   ├── config.js               # API_BASE_URL resolution (env / build-time / default)
│   ├── router.js               # hash-based SPA routing
│   ├── theme.css               # shared dark theme + design tokens
│   ├── utils.js                # esc(), debounce, format, ids
│   └── pages/                  # JS page modules rendered into the shell
│       ├── chat.js             # Chat portal
│       ├── graph.js            # unified 2D/3D graph explorer
│       ├── ingest.js           # upload / crawl
│       └── admin.js            # Sources / Jobs / Stats / Tickets
├── assets/
└── (removed: graph_viewer.html, graph_viewer_3d.html — folded into src/pages/graph.js)
```

> **Deviation from the original draft:** pages are JS modules (`src/pages/*.js`) rendered into the single shell rather than separate `pages/*.html` files, because separate HTML pages cannot share the app shell (and `fetch` of local partials is blocked over `file://`). The app is opened via `frontend/index.html`.

---

## 3. Full list of things that can be added

### A. Chat — Customer Portal (biggest gap)
1. Chat window with message bubbles (user / assistant roles, markdown-light rendering).
2. Thread management: create `thread_id`, list + resume previous threads (persist to `localStorage`).
3. Multi-turn interrupt support (email collection / escalation confirmation) — automatic via `thread_id`.
4. Typing indicator, error banner, retry-send button, request timeout handling.
5. `trace_id` shown in an expandable "debug" panel.
6. (Backend-dependent) citation/source panel if the `/chat` contract later exposes sources.

### B. Knowledge Graph Explorer — enhancements & unification
7. Merge both graph pages into one `pages/graph.html` with a 2D/3D toggle (reuse `graph_viewer_3d.html` logic).
8. Search by **value** via unused `/graph/search_value`.
9. Filter search by entity type via unused `entity_type` param; expose a type dropdown.
10. Filter visible nodes/edges by entity type in-canvas.
11. Layout controls — the unified viewer's 2D/3D engines are **force-graph (d3-force)**, so expose force parameters (charge/repulsion, link distance, gravity, collision radius) instead of named cytoscape layouts (`cose`/`dagre`/`grid` are cytoscape-only). Keep `graph_viewer.html`'s cytoscape as an optional alternate 2D engine only if named layouts are a hard requirement.
12. Export graph as PNG (canvas capture) + JSON; SVG is not native to canvas engines (optional extra dependency or server-side render).
13. Deep-link/share current graph state via URL hash (node set, layout, selection).
14. Relation-type legend with color coding per relation; edge-type filter.
15. Graph statistics panel (entity counts by type, relation counts, density).
16. Mini-map, keyboard shortcuts (esc clears selection, arrows pan), fullscreen mode.
17. Collapsible sidebar; breadcrumb for the active path result.
18. Node detail: render fact values by `value_type` (date/number/json) + copy-to-clipboard.

### C. Document Ingestion UI
19. Drag-and-drop upload form (PDF/DOCX/MD) → `/ingest/upload`, with size/type validation.
20. URL crawl form → `/ingest/crawl`, with URL validation.
21. Knowledge-category selector (passed as `category_id`).
22. Result card showing returned `job_id` / `source_id` / `version_id` and `duplicate_skipped` state.

### D. Admin / Dashboard (partially backend-blocked)
23. Knowledge-source list + current-version status (needs `/admin/knowledge-sources` deps wired + a GET list endpoint).
24. Ingestion job monitor (needs a new GET job-status endpoint — currently none exists).
25. Graph health / stats panel (entity & relation counts via a new summary endpoint or `/search?limit=1` heuristics).
26. Ticket list view (Ticket table exists in `db/models.py:318`; needs a GET endpoint).
27. Chat interaction log (needs a new endpoint; checkpointer state is not queryable today).

### E. Platform / App Shell
28. SPA with hash routing across Chat / Graph / Ingest / Admin.
29. Configurable API base URL (env var / build config / settings page) — kill hardcoded `127.0.0.1:8000`.
30. Shared API client: JSON handling, HTTP error mapping, request timeout, retry with backoff, CORS-safe headers.
31. Responsive layout + mobile pass; consistent dark theme (reuse existing tokens).
32. Authentication (JWT from `auth/jwt.py`) — login screen, token storage, auth header injection. Backend wiring required.
33. SSE/WebSocket streaming of chat replies (backend currently returns full responses only — backend-dependent).

---

## 4. Implementation plan (phased)

### Phase 0 — Foundations (app shell + shared client)

**Goal:** single entry page, working navigation, and one shared API layer everything else builds on.

Tasks:
- [ ] 0.1 Create `src/config.js` — resolve `API_BASE_URL` from (highest priority first): `localStorage` override → `window.__APP_CONFIG__` injected at serve time → env/default `http://127.0.0.1:8000`.
- [ ] 0.2 Create `src/api.js` — `api.get(path, params)`, `api.post(path, body)`, `api.upload(path, file, fields)`; JSON parse, error mapping (4xx/5xx → typed messages), 10s default timeout, exponential-retry for `502/503`, optional auth header injection (Phase 5).
- [ ] 0.3 Create `src/utils.js` — `esc()`, `debounce()`, `formatBytes()`, `formatDate()`, `parseDate()`, `uuid()`.
- [ ] 0.4 Create `src/theme.css` — shared design tokens (bg/panel/text/accent/border), button/label/input/card/chip/suggest-dropdown styles lifted from the existing pages.
- [ ] 0.5 Create `src/router.js` — hash router (`#/chat`, `#/graph`, `#/ingest`, `#/admin`), layout container with persistent header nav.
- [ ] 0.6 Create `index.html` — shell: header (brand + nav links), `#view` outlet, footer status strip.
- [ ] 0.7 Create stub `pages/*.html` for each route (empty page body, placeholder text).
- [ ] 0.8 **Verify:** open `index.html`, navigate all four routes, confirm `config.js` reports the resolved base URL.

### Phase 1 — Chat portal (highest value; backend fully ready)

**Goal:** a production-usable conversational customer portal against `POST /chat`.

Tasks:
- [ ] 1.1 Build `pages/chat.html` — layout: message scroll area + composer + thread sidebar.
- [ ] 1.2 Thread model — `Thread { id, title (first message), created_at, updated_at, messages[] }`; persist list to `localStorage` (`chat.threads.v1`); restore on load.
- [ ] 1.3 Send flow — POST `/chat` with `{thread_id, message}`; optimistic user bubble; typing indicator while pending; append assistant `reply` on success.
- [ ] 1.4 Error handling — HTTP/network/timeout → inline error bubble with **Retry** (re-sends last message to same `thread_id`) and "Start new thread" fallback.
- [ ] 1.5 Multi-turn resume — always reuse the same `thread_id` for a thread; verify a fresh message after an `interrupt` reply returns the resumed flow (email collection → ticket confirmation).
- [ ] 1.6 Message rendering — preserve newlines; linkify URLs; light markdown (bold/italic/code) via a small safe renderer; no raw HTML injection (`esc()` everywhere).
- [ ] 1.7 Thread controls — "New conversation" button (new uuid), delete thread, rename (title stays first message by default).
- [ ] 1.8 Debug panel — expandable row under each assistant reply showing `trace_id`; show raw API error payload.
- [ ] 1.9 Empty/welcome state — greeting + example prompts; disable send while a request is in flight.
- [ ] 1.10 **Verify (manual):** start thread → ask knowledge question → verify grounded answer; ask "create a ticket" → reply asks for email → send email → ticket confirmation; confirm both runs share one `thread_id` and history persists across reload.

### Phase 2 — Graph explorer consolidation + enhancements

**Goal:** one unified viewer replacing both HTML files, with the unused backend endpoints surfaced.

Tasks:
- [ ] 2.1 Create `pages/graph.html` — port `graph_viewer_3d.html` (2D/3D toggle, HUD, legend, detail, path A/B, expand all, auto-rotate) into the app shell.
- [ ] 2.2 Move graph engine loading behind config — CDN URLs in `config.js` with local-file fallbacks; keep graceful degradation banners when engines fail to load.
- [ ] 2.3 Add **value search** — a mode toggle on the search box: "by name" (`/search`) vs "by value" (`/search_value`).
- [ ] 2.4 Add **entity-type filter** on search — a dropdown populated from a `/search?limit=200` type scan (or a fixed list from the ontology) sent as `entity_type`.
- [ ] 2.5 Add **in-canvas type filter** — toggles to show/hide node types and edge (relation) types; recompute layout after filtering.
- [ ] 2.6 Add **relation-type legend** — color code edges by `relation_type`; tooltip label on hover; filter by relation.
- [ ] 2.7 Add **layout controls** — since 2D and 3D are force-graph (d3-force), expose force parameters (charge/repulsion, linkDistance, gravity, collision radius) with live re-run; add `forceAtlas2`-style variants in 3D if available. (Named layouts like `cose`/`dagre`/`grid` are cytoscape-only — only applicable if cytoscape is kept as an alternate 2D engine.)
- [ ] 2.8 Add **export** — PNG (canvas `toDataURL` capture) and JSON (current `{nodes, links}` state); SVG optional (not native to canvas engines — extra dependency or server-side).
- [ ] 2.9 Add **deep-linking** — serialize current graph state (seeded ids, layout, mode, type filters) into the URL hash; restore on load.
- [ ] 2.10 Add **stats panel** — counts by entity type, total nodes/edges, graph density; reuse existing legend counts and HUD.
- [ ] 2.11 Add **mini-map + keyboard shortcuts** — minimap toggle, `Esc` deselect, `Delete` clears, `f` fit/framed, fullscreen button.
- [ ] 2.12 Improve **detail panel** — render facts by `value_type` (date, number, json pretty), copy-to-clipboard, link from fact values to entity search when applicable.
- [ ] 2.13 **Remove** `graph_viewer.html` + `graph_viewer_3d.html` once parity is confirmed (redirect old links to `#/graph`).
- [ ] 2.14 **Verify:** seed, expand, path A/B, all three search modes, type filtering, export, deep-link round-trip in both 2D and 3D.

### Phase 3 — Document Ingestion UI

**Goal:** let admins/operators add knowledge-base content from the browser.

Tasks:
- [ ] 3.1 Create `pages/ingest.html` — two cards: "Upload file" and "Crawl URL".
- [ ] 3.2 Upload form — drag-and-drop + file picker; accept `application/pdf`, DOCX, `text/markdown`; client-side type/size validation mirroring `api/ingest.py` (surface 415 unsupported-type / 400 empty-file as messages).
- [ ] 3.3 Crawl form — URL input with `http(s)` validation; submit → `POST /ingest/crawl` (surface 400 fetch-failure / 502 crawl-failure).
- [ ] 3.4 Category selector — optional `category_id`; fetched from a category endpoint if one is added (else hidden, sent as `null`).
- [ ] 3.5 Result card — on `submitted` render `{job_id, source_id, version_id}`; on `duplicate_skipped` the API returns **only** `{status}` (no IDs) — render a "duplicate, nothing indexed" state; copy IDs; link to graph search for the new source when possible.
- [ ] 3.6 In-flight states — progress note "ingesting… (synchronous, may take several seconds)", disable double-submit.
- [ ] 3.7 **Verify:** upload a PDF and a markdown file (expect `submitted`), re-upload the same file (expect `duplicate_skipped`), crawl a public URL.

### Phase 4 — Admin / Dashboard (backend work first)

**Goal:** operational visibility. **Blocked on small backend additions** — do not build UI against placeholder endpoints.

Required backend wiring (out of frontend scope, listed for sequencing):
- [ ] 4.0a Wire `/admin/knowledge-sources` dependencies in `main.py` (replace `NotImplementedError` placeholders in `storage/api.py`).
- [ ] 4.0b Add `GET /admin/knowledge-sources` (list sources + current version status).
- [ ] 4.0c Add `GET /admin/jobs` (recent `KnowledgeInjectionJob` rows with status/counts/errors).
- [ ] 4.0d (optional) Add `GET /admin/stats` (entity/relation counts by type) and `GET /admin/tickets` (list `Ticket` rows).

Tasks:
- [ ] 4.1 Create `pages/admin.html` — tabs: Sources / Jobs / Stats / Tickets.
- [ ] 4.2 Sources tab — table (name, type, category, current version status, updated_at, active); status badges colored per `VersionStatusEnum` (PENDING/PROCESSING/INDEXED/FAILED/STALE/ARCHIVED).
- [ ] 4.3 Jobs tab — table (job_id, type, status, counts, error_details, timestamps); auto-refresh poll every 10s; error-detail expander.
- [ ] 4.4 Stats tab — charts/panels: entity counts by type (bar), relation counts by type, totals; refresh button.
- [ ] 4.5 Tickets tab — table (id, email, query, priority, status, created_at).
- [ ] 4.6 **Verify:** against docker stack with ingested documents; job states transition QUEUED → RUNNING → SUCCEEDED/FAILED.

### Phase 5 — Polish, auth, streaming, deployment

Tasks:
- [ ] 5.1 Responsive pass — mobile chat layout (sidebar collapses to drawer), touch-friendly graph controls.
- [ ] 5.2 Settings page — API base URL override (persisted to `localStorage`), engine-CDN override, theme accent picker.
- [ ] 5.3 Keyboard/UX polish — global shortcuts, empty/error states everywhere, accessible focus outlines, ARIA labels on interactive graph nodes.
- [ ] 5.4 Auth — login screen using `auth/jwt.py` (backend must register auth + protected routes first); store token; inject `Authorization` header in `api.js`; 401 → redirect to login.
- [ ] 5.5 Streaming — if backend adds SSE/WebSocket chat streaming, extend the chat composer to render tokens incrementally.
- [ ] 5.6 Deployment — confirm docker volume mount serves `frontend/` unchanged; document run steps in README; (optional) CI build if a framework is chosen.
- [ ] 5.7 **Final verification** — full end-to-end walkthrough: chat ticket flow, graph exploration, ingestion, admin monitoring.

---

## 5. Backend dependencies (needed before/with certain phases)

| Backend change | Unlocks | Blocking? |
|---|---|---|
| Wire `/admin/knowledge-sources` deps + GET list | Phase 4 Sources tab | Yes (Phase 4) |
| New `GET /admin/jobs` | Phase 4 Jobs tab | Yes (Phase 4) |
| New `GET /admin/stats`, `GET /admin/tickets` | Phase 4 Stats/Tickets tabs | Optional |
| Register auth + protect routes (`auth/jwt.py`) | Phase 5.4 login | Optional |
| SSE/WebSocket chat streaming | Phase 5.5 | Optional |
| Expose `/chat` sources/citations | Chat citations panel (item 6) | Optional |

---

## 6. Backend files that must change (to match this plan)

### 6.1 Required for Phase 0 — blockers (every page needs these)

| File | Change |
|---|---|
| `backend/src/ai_customer_assistant/main.py` | Add `CORSMiddleware` (`allow_origins=["*"]` or explicit origins) — **none exists today**, `file://`/cross-origin pages are browser-blocked. Mount static files (`app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True))`) so the app shell is served same-origin (uses the already-set `FRONTEND_DIR` env, currently unused). `include_router()` the new admin + auth routers. |

### 6.2 Required for Phase 4 — Admin dashboard

| File | Change |
|---|---|
| `backend/src/ai_customer_assistant/ingestion/storage/api.py` | Replace the three `NotImplementedError` dependency placeholders (`get_storage_config`, `get_db_session`, `get_current_admin_user_id`, lines 62–83) with real wiring (config singleton on `app.state`, reuse `db/async_session.py:get_session`, admin-user dep). Add `GET /admin/knowledge-sources` (list sources + current version status). |
| `backend/src/ai_customer_assistant/api/admin.py` *(new)* | `GET /admin/jobs` — recent `KnowledgeInjectionJob` rows (status, counts, error_details, timestamps). Optional: `GET /admin/stats` (entity/relation counts by type), `GET /admin/tickets` (list `Ticket` rows from `db/models.py:318`). |

### 6.3 Required for Phase 2 / 3 — small additions

| File | Change |
|---|---|
| `backend/src/ai_customer_assistant/api/graph.py` | Add `GET /graph/entity-types` — the ontology's entity-type list (populates the type-filter dropdown; avoids a drifting hardcoded copy). Add `GET /graph/categories` — lists `knowledge_category` rows (unlocks the ingestion category selector; `category_id` is accepted as input but no list endpoint exists). |

### 6.4 Optional / later phases

| File | Change |
|---|---|
| `backend/src/ai_customer_assistant/services/chat_service.py` | For the chat citation panel (item 6): return `citations` (available in `knowledge_response`, currently dropped — `handle_message` returns only the reply string, `chat_service.py:116`). |
| `backend/src/ai_customer_assistant/api/routes.py` | Alongside the above: extend `ChatResponse` with `citations`/`sources` fields. |
| `backend/src/ai_customer_assistant/auth/jwt.py` + new `api/auth.py` | `jwt.py` is empty today — implement JWT helpers + a login endpoint + a `Depends` auth guard, protect `/chat` and admin routes (Phase 5.4). |
| `backend/src/ai_customer_assistant/api/stream.py` *(new)* | SSE/WebSocket chat streaming (Phase 5.5); registered `/chat` currently returns full responses only. |
| `backend/src/ai_customer_assistant/api/chat.py` | Not on the critical path — this alternate `{message, history}` contract is unregistered. Either leave as dead code or delete it (the plan targets `routes.py`'s `/chat`). |

### 6.5 Summary

- **Hard blockers:** `main.py` (CORS + static mount); `storage/api.py` + `api/admin.py` (Phase 4 only).
- **Small adds:** `api/graph.py` (two GET endpoints).
- **Optional:** `chat_service.py` + `routes.py` (citations), auth wiring, `api/stream.py` (streaming), `api/chat.py` cleanup.
- **Note:** `api/graph.py:51-52` also builds its own separate DB engine (documented ASSUMPTION in its docstring) — not a frontend blocker, but worth consolidating with `db/async_session.py` when touching that file.

## 7. Out of scope for this plan

- Re-architecting the backend agents or graph pipeline.
- Any mutation endpoints beyond the existing `/chat`, `/ingest/*` (admin write endpoints are future backend work).
- Multi-tenant / org separation in the frontend.
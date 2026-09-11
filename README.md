# AI Customer Assistant

A retrieval-augmented customer-support assistant. A LangGraph **Supervisor**
classifies each message and routes it to one of two agents:

- the **Knowledge Agent** — a full RAG subgraph (rewrite → extract → choose a
  retrieval strategy → structured lookup and/or pgvector similarity search →
  rank → dedupe → build context → answer), grounded in documents ingested from
  uploads or a website crawl;
- the **Ticket Agent** — a multi-turn flow that asks why a ticket is needed,
  collects an email address, writes the ticket, and emails a confirmation.

Ingested documents are stored twice: as 768-dimension embeddings in
`embedding_chunk` (pgvector) for semantic search, and as an entity /
attribute / value / relation knowledge graph for exact structured lookups.
A question that names something the graph knows searches **both at once**,
concurrently, and the answer prompt gets the two as separate sections.

## The chat API

| Endpoint | Shape |
|---|---|
| `POST /chat/stream` | Server-sent events: a `trace_id`, then progress stages, heartbeats, and the answer. What the UI uses — a turn takes tens of seconds and this is what makes that legible. |
| `POST /chat` | One buffered JSON response. Unchanged, and the fallback when streaming is unavailable. |

Both take `{"thread_id": ..., "message": ...}` and produce the same answer;
history lives server-side behind the checkpointer, keyed by `thread_id`.

**Both require a signed-in `member`**, as does everything else except
`/health` and the login endpoints — see [Authentication](#authentication)
below.

---

## Requirements

| Tool | Notes |
|---|---|
| Docker + Docker Compose | Runs Postgres 16 (+pgvector), MinIO, Apache Tika, the API, and the ingestion worker |
| Python 3.12 | Only for running the API or the tests on the host |
| [uv](https://docs.astral.sh/uv/) | Dependency management |
| Chromium (via Playwright) | Only for crawling; see [Crawling a website](#crawling-a-website) |

A Groq API key is required for real answers. Without one the app still runs —
the provider resolver falls back to a deterministic stub.

---

## First run

```bash
# 1. Configuration. Both files are gitignored; never commit a filled-in .env.
cp backend/.env.example backend/.env    # then add your GROQ_API_KEY
cp backend/.env.example .env            # docker compose reads APP_PORT from here

# 2. Start Postgres, MinIO, Tika, the API and the worker.
make up

# 3. Apply the database schema.
cd backend && uv run alembic upgrade head && cd ..

# 4. Create the service account that ingestion attributes documents to.
make user

# 5. Set a signing secret and create the first admin account.
#    AUTH_SECRET has no default: the app refuses to start without it.
python -c "import secrets; print('AUTH_SECRET=' + secrets.token_urlsafe(48))" >> backend/.env
cd backend && uv run python scripts/create_user.py you@example.com --role admin && cd ..

# 6. Open the chat UI and sign in. `make` reads the port from APP_PORT in ./.env.
make frontend
```

`make up` builds the backend image on first run, which downloads the
`BAAI/bge-base-en-v1.5` embedding model (~400 MB). Expect the first start to
take several minutes; `make status` shows when every container is healthy.

### Two `.env` files, on purpose

`backend/.env` configures the application. The root `.env` is read by Docker
Compose itself for `APP_PORT` and the Postgres credentials it creates the
database with. Inside a container the environment comes from Compose —
`backend/.env` is deliberately excluded from the image by `.dockerignore`, so
`POSTGRES_HOST=postgres` (the service name) is correct there while
`localhost` is correct on the host.

---

## Everyday commands

Run `make help` for the full list.

| Command | What it does |
|---|---|
| `make up` / `make down` | Start / stop the whole stack |
| `make status` | Container health |
| `make logs` | Tail all container logs |
| `make uvicorn` | Run the API on the **host** with `--reload`, on port 8002 |
| `make worker` | Run the ingestion worker on the host |
| `make test` | Run the backend test suite |
| `make frontend` | Open the chat UI |
| `make graph` | Open the knowledge-graph explorer |
| `make ingest` | Crawl and ingest a URL (prompts for the URL) |
| `make verify` | List ingested knowledge sources |
| `make psql` | Open a `psql` shell on the running database |
| `make trunc` | Empty every knowledge table, keeping the schema |
| `make clean` | Stop containers **and delete the Postgres and MinIO volumes** |

`make uvicorn` and `make worker` invoke `backend/.venv`'s interpreter by
absolute path. That is deliberate: an activated conda environment or a stale
`VIRTUAL_ENV` from another checkout otherwise shadows the project's packages,
and the app dies with `ModuleNotFoundError: No module named 'pgvector'`.

---

## Authentication

Every endpoint requires a signed-in account except three: `GET /health`,
`POST /auth/login` and `POST /auth/refresh`. `tests/api/test_route_protection.py`
enumerates the assembled app and asserts exactly that, so a route added later
is protected by default or the suite fails.

### Two roles

| Role | Can |
|---|---|
| `member` | Chat, browse the knowledge graph, upload documents, run crawls, watch jobs. Sidebar: Overview, Chat, Graph, Ingest. |
| `admin` | All of that, plus the system's own controls: creating accounts, agent prompts, **LLM provider API keys**, the Admin dashboard (sources, jobs, stats, tickets), and — when they are built — retrieval configuration and source deletion. Sidebar adds Prompt, Admin and API Keys. |

The UI hides admin pages from a member and redirects with a message if one is
reached by URL. That is a convenience, not the protection — the API refuses
unauthorised requests on its own, which is what holds when someone skips the
browser.

The line is *uses the system* vs. *changes the system*. Across the endpoints
that exist today the only live difference is account creation; the split
exists so the administrative endpoints still to be written have somewhere to
land that is not the same gate as uploading a PDF.

### Accounts

There is no self-signup. The first admin is created from the command line,
and every account after that through the API:

```bash
cd backend
uv run python scripts/create_user.py sam@example.com --role member
uv run python scripts/create_user.py sam@example.com --update     # reset a password
```

The password is prompted for, never passed as an argument — an argument ends
up in shell history and in `ps` output. Use `--password-stdin` in a
provisioning script.

### From a script

The browser gets `HttpOnly` cookies and needs nothing. For `curl`, ask for
the bearer transport and send the token as a header:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"...","transport":"bearer"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -H "Authorization: Bearer $TOKEN" localhost:8000/graph/search?q=alpinist
```

Under the default `transport: "cookie"` the tokens are set as cookies and are
**not** in the response body, so no script — including one injected into the
page — can read them.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `AUTH_SECRET` | *none* | **Required.** The process refuses to start without it; at least 32 characters. Changing it signs everyone out. |
| `AUTH_COOKIE_SECURE` | `true` | Leave it on. `http://localhost` still works — browsers treat it as trustworthy. Set `false` only to reach the app over plain HTTP at a LAN address. |
| `CORS_ALLOW_ORIGINS` | empty | Empty is correct: the frontend is served same-origin, so no CORS is involved. A wildcard is not accepted — the session is a cookie. |
| `CRAWL_DOMAIN_ALLOWLIST` | empty | Restrict crawling to these domains. Empty means any publicly-routable host. |
| `CRAWL_ALLOW_ADDRESSES` | empty | CIDRs exempt from the private/loopback refusal, for crawling an intranet. A list, not a switch, so opening `10.1.0.0/16` does not also open `169.254.169.254`. |
| `RATE_LIMIT_DISABLED` | `false` | Tests and single-user local development only. |
| `GROQ_API_KEY` and friends | unset | Still read, and still the fallback. A key saved on the **Admin › API Keys** page shadows the matching variable; clearing it falls back here. Stored keys are encrypted under a key derived from `AUTH_SECRET`, so rotating that secret means re-entering them. |

Rate limits: 5 logins per 15 minutes (per address *and* per account), 20 chat
turns a minute and 500 a day per user, 10 ingestion calls a minute. The chat
limits matter most — every turn spends Groq tokens against a daily budget
that one developer exhausted repeatedly during this project's own testing.

Full design and rationale: [`authentication_implementation.md`](authentication_implementation.md).

---

## Ingesting documents

Nothing can be answered until something is ingested. Every path below only
*enqueues* a job — the worker does the real work, so watch `make logs` or poll
`GET /ingest/jobs/{job_id}`. The examples below assume `APP_PORT` is exported from `./.env`.

**Upload a file** (PDF, DOCX or Markdown):

```bash
curl -F 'file=@handbook.pdf' http://127.0.0.1:$APP_PORT/ingest/upload
```

**Crawl a single page:**

```bash
curl -X POST http://127.0.0.1:$APP_PORT/ingest/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/docs", "scope": "PAGE"}'
```

### Crawling a website

Site crawls render pages with Playwright, which needs a Chromium binary that
`uv sync` does not install:

```bash
cd backend
uv run playwright install --with-deps chromium
```

A site crawl is always two steps, so nothing is ingested without review:

```bash
# 1. Discover. Returns a discovery_id and the list of pages found.
curl -X POST http://127.0.0.1:$APP_PORT/ingest/crawl/discover \
  -H 'Content-Type: application/json' -d '{"root_url": "https://example.com"}'

# 2. Confirm. Crawls and ingests that list.
curl -X POST http://127.0.0.1:$APP_PORT/ingest/crawl/{discovery_id}/confirm \
  -H 'Content-Type: application/json' -d '{}'
```

Discoveries expire after 10 minutes.

---

## Running the tests

```bash
make test                                  # whole suite
make test PYTEST_ARGS='-q tests/agents'    # one directory
```

**969 passing, no failures or errors.** Use `make test` rather than a bare
`pytest`: an activated conda environment shadows the project's interpreter and
produces two dozen spurious collection errors. `make test` invokes
`backend/.venv`'s Python by absolute path.

Tests that need a live Postgres or a Groq key skip themselves when those are
absent, so a bare checkout still goes green. The crawler tests additionally
need a Chromium binary:

```bash
cd backend && uv run playwright install chromium
```

---

## Layout

```
backend/
  src/ai_customer_assistant/
    agents/supervisor/     classification and routing; the top-level graph
    agents/knowledge/      the RAG subgraph — rewrite, retrieve, rank, answer
    agents/ticket_agent/   ticket creation, persistence and email
    ingestion/             crawler, Tika extraction, chunking, embedding,
                           EAV extraction, and the job queue
    ontology/              the entity/attribute/relation vocabulary shared by
                           ingestion and retrieval
    api/                   FastAPI routers (chat, graph, ingest)
    auth/                  tokens, passwords, roles, route dependencies,
                           the /auth router, rate limiting, the SSRF guard
    db/                    SQLAlchemy models, the shared engine, checkpointer
  alembic/versions/        database migrations
  tests/                   the test suite, mirroring the source layout
frontend/                  the chat UI and knowledge-graph explorer
status.md                  detailed project status, known problems, roadmap
```

---

## Which documents to trust

| File | What it is |
|---|---|
| `status.md` | **Current.** What works, what does not, every fixed and open problem with the evidence behind it. Start here before changing anything substantial. |
| `test.md` | **Current.** A live end-to-end test of the running app through a real browser, and the nine defects it found. Read it for how the system behaves under load rather than in tests. |
| `CHANGELOG.md` | **Current.** Release-shaped summary of the same work. |
| `authentication_implementation.md` | **Current.** The authentication design (P0-3) and, in §15, the five places the implementation ended up differing from the plan. Read it before changing anything in `auth/`. |
| `frontend/crawler_frontend_integration.md` | **Current.** Every ingestion endpoint in it was re-verified against `api/ingest.py`, and it was updated for authentication on 2026-09-08. |
| `docs/architecture.md`, `docs/agents_integration_plan*.md`, `docs/agent implementation and integration.md`, `frontend/frontend_plan.md` | **Historical.** The original design and planning documents, each now carrying a banner saying so. They describe what was *intended*, not what was built — several decisions were later made differently, and a few were reversed with evidence. Kept as a record. |
| `docs/pricing.md` | Not documentation — it is source content for the knowledge base. |

Configuration is documented in `status.md` §9 — the timeout ladder, retrieval
thresholds, authentication, crawler safety and logging controls. All have
working defaults except `AUTH_SECRET`, which deliberately has none: the
process refuses to start without it.

---

## Known limitations

**One residual gap in the SSRF guard, written down rather than papered
over.** The crawler validates a URL, resolves it, refuses anything not
publicly routable, and re-checks the address actually connected to before
using the response — but it cannot *pin* the connection to the address it
validated, because `httpx` has no supported way to do that and the workaround
breaks certificate verification. An attacker with an account can therefore
still cause one *blind* request to an internal address; they cannot see the
answer. `auth/ssrf.py` documents this in full.

**Answer latency is tens of seconds**, dominated by four sequential LLM
calls, not by retrieval — retrieval measures 0.14–0.24 s. `/chat/stream`
makes that legible rather than shorter.

**The free-tier Groq quota is shared between ingestion and chat.** A worker
draining an ingestion backlog will make the assistant return "I'm handling
more requests than I can keep up with". Stop the worker when demonstrating
the chat.

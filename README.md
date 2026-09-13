# AI Customer Assistant

A retrieval-augmented customer-support assistant. A LangGraph **Supervisor**
classifies each message and routes it to one of three destinations:

- the **Knowledge Agent** — a full RAG subgraph (rewrite → extract → choose a
  retrieval strategy → structured lookup and/or pgvector similarity search →
  rank → dedupe → build context → answer), grounded in documents ingested from
  uploads or a website crawl;
- the **Ticket Agent** — a multi-turn flow that asks why a ticket is needed,
  collects an email address, writes the ticket, and emails a confirmation;
- **ticket status lookup** — answers "what happened to my ticket?" from the
  ticket id. Separate from the Ticket Agent on purpose: that one's whole job
  is *creating* a ticket, so sending a status question there would open a
  second one.

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
#    The API container applies the database migrations itself on startup.
make up

# 3. Create the service account that ingestion attributes documents to.
make user

# 4. Set a signing secret and create the first admin account.
#    AUTH_SECRET has no default: the app refuses to start without it.
python -c "import secrets; print('AUTH_SECRET=' + secrets.token_urlsafe(48))" >> backend/.env
cd backend && uv run python scripts/create_user.py you@example.com --role admin && cd ..

# 5. Open the chat UI and sign in. `make` reads the port from APP_PORT in ./.env.
make frontend
```

**Applying a migration by hand** — you do not need this on first run, only
after writing one and wanting it applied without restarting the API:

```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH ./.venv/bin/python -m alembic upgrade head
```

Both overrides are required from the host: `backend/.env` says
`POSTGRES_HOST=postgres`, which is the Compose service name and resolves only
inside the network, and Compose publishes Postgres on **5433** to avoid
colliding with a local install. `env -u PYTHONPATH ./.venv/bin/python` rather than
`uv run alembic`, which fails here with
`ModuleNotFoundError: No module named 'pgvector'` — the same interpreter
shadowing `make uvicorn` works around, described below.

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

Every endpoint requires a signed-in account except six: `GET /health`,
`POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` — logout is open
because an expired session must still be able to revoke itself — and
`POST /chat` plus `POST /chat/stream`, which are the public front door. `tests/api/test_route_protection.py`
enumerates the assembled app and asserts exactly that, so a route added later
is protected by default or the suite fails.

### Three roles

| Role | Can |
|---|---|
| *(nobody)* | **Chat is public.** A prospective client opens the URL and starts asking — no account, no signup, no login screen. They get a dedicated surface with no workspace chrome: an assistant identity bar, the conversation, a composer. `visitor` survives as the floor of the role ordering, but nothing creates such accounts. |
| `member` | Chat, browse the knowledge graph, upload documents, run crawls, watch jobs. A colleague, **created by an admin** rather than self-service. Sidebar: Overview, Chat, Graph, Ingest. |
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

**There is no self-signup.** Nobody needs an account to use the assistant —
chat is the front door. Accounts exist for staff, and an admin creates them.
The login page is reached from a quiet "Staff sign in" link in the header,
never by being bounced there.

Admin accounts are never self-service. The first is created from the command
line, and every one after that through the admin API:

```bash
cd backend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 uv run python scripts/create_user.py sam@example.com --role member
POSTGRES_HOST=localhost POSTGRES_PORT=5433 uv run python scripts/create_user.py sam@example.com --update   # reset a password
```

The two overrides are needed for the same reason as applying a migration by
hand: `backend/.env` says `POSTGRES_HOST=postgres`, which resolves only inside
the Compose network, so without them the script fails with
`failed to resolve host 'postgres'`.

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
| `SIGNUP_MAX_PER_HOUR` | `10` | Member accounts one IP may create per hour. Coarser than it looks — behind Docker every request from the host machine arrives as the gateway address, so one bucket covers that whole machine, and rejected attempts count too. Raise it for local development. |
| `RATE_LIMIT_DISABLED` | `false` | Tests and single-user local development only. |
| `GROQ_API_KEY` and friends | unset | Still read, and still the fallback. A key saved on the **Admin › API Keys** page shadows the matching variable; clearing it falls back here. Stored keys are encrypted under a key derived from `AUTH_SECRET`, so rotating that secret means re-entering them. |

Rate limits: 5 logins per 15 minutes (per address *and* per account), 10
signups an hour per address, 20 chat turns a minute and 500 a day per user,
10 ingestion calls a minute. The chat
limits matter most — every turn spends Groq tokens against a daily budget
that one developer exhausted repeatedly during this project's own testing.

Full design and rationale: [`authentication_implementation.md`](authentication_implementation.md).

---

## Ingesting documents

Nothing can be answered until something is ingested. Every path below only
*enqueues* a job — the worker does the real work, so watch `make logs` or poll
`GET /ingest/jobs/{job_id}`. The examples below assume `APP_PORT` is exported from `./.env`.

The worker processes two documents at once (`PGQUEUE_CONCURRENCY`, sharing one
copy of the embedding model) and every log line carries the run that produced
it, as `[<job>.<attempt>]`. A job that fails for a transient reason — a rate
limit, a Tika restart, a worker that died mid-job — goes back on the queue with
a backoff and is retried up to four times, then lands in `DEAD_LETTER`, which
is deliberately distinct from `FAILED`: the first means "this kept failing for
a reason that usually passes", the second means "this cannot work as it
stands". Admin › Jobs shows both, with the attempt count and failure kind.

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
    api/                   FastAPI routers (chat, graph, ingest, admin)
    auth/                  tokens, passwords, roles, route dependencies,
                           the /auth router, rate limiting, the SSRF guard
    db/                    SQLAlchemy models, the shared engine, checkpointer
  alembic/versions/        database migrations
  tests/                   the test suite, mirroring the source layout
frontend/                  chat, knowledge-graph explorer, ingest, prompts,
                           login, and the admin pages (sources, jobs, stats,
                           tickets, API keys)
status.md                  detailed project status, known problems, roadmap
ingestion.md               the ingestion pipeline in depth — how it works,
                           every problem found in it and how each was fixed
```

---

## Which documents to trust

| File | What it is |
|---|---|
| `status.md` | **Current.** What works, what does not, every fixed and open problem with the evidence behind it. Start here before changing anything substantial. |
| `ingestion.md` | **Current.** The ingestion pipeline end to end, and the eighteen problems found in it — duplicate chunks, no retry anywhere, one LLM call per window, superseded facts, entities fragmenting across type labels — each with the evidence and the fix. Read it before changing anything under `ingestion/`. |
| `test.md` | **Current.** A live end-to-end test of the running app through a real browser, and the nine defects it found. Read it for how the system behaves under load rather than in tests. |
| `CHANGELOG.md` | **Current.** Release-shaped summary of the same work. |
| `authentication_implementation.md` | **Current.** The authentication design (P0-3) and, in §15, the five places the implementation ended up differing from the plan. Read it before changing anything in `auth/`. |
| `frontend/crawler_frontend_integration.md` | **Current.** Every ingestion endpoint in it was re-verified against `api/ingest.py`, and it was updated for authentication on 2026-09-08. |
| `docs/architecture.md`, `docs/agents_integration_plan*.md`, `docs/agent implementation and integration.md`, `frontend/frontend_plan.md` | **Historical.** The original design and planning documents, each now carrying a banner saying so. They describe what was *intended*, not what was built — several decisions were later made differently, and a few were reversed with evidence. Kept as a record. |
| `docs/pricing.md` | Not documentation — it is source content for the knowledge base. |

Configuration is documented in `status.md` §9 — the timeout ladder, retrieval
thresholds, authentication, crawler safety and logging controls — and the
ingestion knobs (worker concurrency, retry attempts, backoff) are in
`backend/.env.example`. All have working defaults except `AUTH_SECRET`, which
deliberately has none: the process refuses to start without it.

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
the chat. It is also the real ceiling on ingestion throughput — the worker's
two lanes stop a slow document blocking the queue, but they cannot create
tokens, so against a daily budget they do not double what gets indexed.

**A name is one entity, whatever its type.** Entity identity is the normalized
name, because keying it on the model-chosen type fragmented the graph badly:
"Agile" was three entities holding 39, 14 and 2 facts. All 62 fragmented names
in this corpus were the same real thing seen through different lenses, with no
genuine homonyms — but a true homonym *would* now merge. In a single-company
knowledge base that is a remote risk and a visible one, where fragmentation
was certain and silent. `ingestion.md` §9 has the full reasoning.

**Superseded facts are kept but not yet answerable.** When a re-ingest stops
asserting a fact, the old value is marked rather than deleted, and retrieval
returns current values only — so contradictions cannot reach an answer. What
is deliberately not built is the temporal query itself: *"what was the previous
rate?"* has the data behind it now, but no way to ask.

**The crawler ingests error pages.** A 404 body is chunked, embedded and
indexed like any other document — this corpus contains one whose text begins
"# Oops!". Nothing downstream can tell it from a real page.

**Only ingestion labels its logs.** Every line from a job carries
`[<job>.<attempt>]`; the chat path threads a `trace_id` through its graph
config but never sets the logging context, so its lines print `-`.

**Chat is public and there is one corpus.** Anyone who finds the URL can ask
anything the corpus answers — no account, no signup, nothing in between. A
corpus review (see `status.md`) found the content publishable: 19 of 24
sources are the company's own website, and the staff names in it come from the
public team page and published testimonials. What is *not* settled is the
`document classification` line on four uploaded PDFs, which reads
"internal / public company information". Resolve that before a public
deployment.

**The spend ceiling is load-bearing.** One turn costs ~6,900 model tokens. Per
caller limits key on the account when there is one and the IP address when
there is not, and an IP is weak — shared behind NAT, rotated in seconds. So
`CHAT_GLOBAL_PER_DAY` counts every turn into one bucket; when it trips the
assistant says it is busy and stops. Without it, a public endpoint that spends
money per request has no floor.

**No CI, no lint or type configuration.** There is no `.github/`, no `ruff`
or `mypy` config, and no coverage gate: the suite is run by hand.

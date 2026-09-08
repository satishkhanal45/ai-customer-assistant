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

# 5. Open the chat UI. `make` reads the port from APP_PORT in ./.env.
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

**638 passing, no failures or errors.** Use `make test` rather than a bare
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
| `frontend/crawler_frontend_integration.md` | **Current.** Every ingestion endpoint in it was re-verified against `api/ingest.py`. |
| `docs/architecture.md`, `docs/agents_integration_plan*.md`, `docs/agent implementation and integration.md`, `frontend/frontend_plan.md` | **Historical.** The original design and planning documents, each now carrying a banner saying so. They describe what was *intended*, not what was built — several decisions were later made differently, and a few were reversed with evidence. Kept as a record. |
| `docs/pricing.md` | Not documentation — it is source content for the knowledge base. |

Configuration is documented in `status.md` §9 — the timeout ladder, retrieval
thresholds, and logging controls all have working defaults and are listed
there because most of them are load-bearing.

---

## Known limitations

**There is no authentication.** Every endpoint is open, CORS allows all
origins, and `POST /ingest/crawl` will fetch any URL it is given. Do not
expose this to the internet as it stands. This is the single blocker between
the project and an internal deployment; `status.md` P0-3 has the detail.

**Answer latency is tens of seconds**, dominated by four sequential LLM
calls, not by retrieval — retrieval measures 0.14–0.24 s. `/chat/stream`
makes that legible rather than shorter.

**The free-tier Groq quota is shared between ingestion and chat.** A worker
draining an ingestion backlog will make the assistant return "I'm handling
more requests than I can keep up with". Stop the worker when demonstrating
the chat.

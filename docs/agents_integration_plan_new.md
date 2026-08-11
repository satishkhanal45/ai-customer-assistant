# Agent Integration Plan — Supervisor · Knowledge · Ticket · Safety

Status: **Phase 1 complete and merged. Phases 2-6 remain planning only.**
Scope: `backend/src/ai_customer_assistant/agents/` plus the layers that will
eventually call it (`services/chat_service.py`, `api/routes.py`, `main.py`).

**Phase 0 exit state:** full suite 176 passed, 1 skipped (up from 146 — the
supervisor test file was never being collected before this phase; see §4.0
implementation notes). `contracts.py`, the optional-injection signature on
`build_supervisor_graph`, and the `MemorySaver` checkpointer are landed and
tested. Phase 1 (wiring the Knowledge Agent) is unblocked.

**Phase 1 exit state:** full suite 187 passed, 1 skipped. `agents/knowledge/__init__.py`
populated with the public API; `make_knowledge_agent_node` landed in the new
`agents/supervisor/agents_wiring.py` (async `ainvoke` under `asyncio.wait_for`,
GroundedResponse-shaped error markers, never a `DownstreamResult`);
`build_supervisor_graph(knowledge_graph=...)` wires the real RAG subgraph in
place of the Knowledge placeholder. See §4.1 for notes. Phase 2 (wiring the
Safety Agent as its own node) is unblocked.

**Revision note:** this is a v2 rewrite of the original integration plan.
It resolves the previously open design questions up front (contract typing,
composition style, multi-turn state) rather than leaving them as
implementation-time decisions, and reorders the phases so foundational
pieces (contracts, composition, persistence) land before any agent gets
wired in. This avoids retrofitting those decisions after code exists.

---

## 1. Current state at a glance

| Agent | Location | Implemented? | Wired into the flow? |
|---|---|---|---|
| Supervisor | `agents/supervisor/` | Yes (classify + route) | Yes — owns the top-level graph |
| Knowledge | `agents/knowledge/` | Yes (full RAG subgraph) | **No** — only a placeholder node in the Supervisor graph |
| Ticket | `agents/ticket_agent/` | Partially (create + validate) | **No** — only a placeholder node |
| Safety | `agents/safety_agent/` | Yes (groundedness + report) | **No** — referenced by design, not wired |

`supervisor/graph.py:69-70` currently registers both downstream agents as
`_placeholder_agent_node(...)`, which returns
`{"status": "NOT_IMPLEMENTED", "response": "<agent> is not yet implemented."}`.
That is the single seam where the real agents plug in.

---

## 2. Work of each agent (unchanged behavior, contract now frozen)

### 2.1 Supervisor Agent — `agents/supervisor/`

**Job:** domain gate, intent classification, and routing. It never retrieves
knowledge and never creates tickets itself; it decides *which* downstream agent
handles a turn, and — in this revision — routing decisions live entirely in
conditional edges, never inside a response-formatting node.

**Public API** (`__init__.py`):
- `build_supervisor_graph(...)` — builds/compiles the top-level LangGraph.
  See §4.2 for the revised signature.
- `SupervisorLLMClient` protocol + `StubSupervisorLLMClient`, `GeminiSupervisorLLMClient`,
  `GroqSupervisorLLMClient`, and `build_llm_client(provider)` (`llm_client.py`).
- `Classification`, `parse_llm_response` (`classification.py`).
- `RoutingDecision`, `decide_route`, `decide_post_downstream` (`routing.py`).
- Types: `SupervisorState`, `Intent`, `NextAgent`, `RequestCategory`, `TicketType`
  (`schema.py`).

**Revised graph topology** (`graph.py`):

```
classify_and_route
   └─ conditional edge
      ├─ END                     (GREETING / OUT_OF_SCOPE / clarification question)
      ├─ knowledge_agent node
      └─ ticket_agent node

knowledge_agent ──> safety_gate ──> decide_post_downstream (conditional edge)
      ├─ FINALIZE  ──> assemble_response ──> END
      └─ ESCALATE  ──> ticket_agent node (re-entrant, see §4.4)

ticket_agent ──> assemble_response ──> END
```

The key change from v1: `decide_post_downstream` is now a **conditional edge**,
not logic buried inside `assemble_response`. `assemble_response` only ever
formats `downstream_result.response` into the final string — it never decides
where control goes next. This is what makes the escalation edge (§4.5) a
one-line addition instead of a workaround.

**State contract** (`schema.py:SupervisorState`):
- Inputs: `user_message`, `conversation_history` (`list[ConversationTurn]`),
  `downstream_result`.
- Supervisor-owned: `request_category`, `domain_confidence`, `intent`,
  `intent_confidence`, `clarification_required`, `clarification_question`,
  `clarification_attempts`, `next_agent`, `ticket_type`, `final_response`.
- **New:** `trace_id: str` — generated once per incoming request, threaded
  through every node for cross-agent log correlation (§4.6).
- **Removed from v1 draft:** `pending_ticket` / `awaiting_escalation_confirmation`
  as raw state fields. Multi-turn state now lives in the graph's checkpointer,
  not in ad-hoc SupervisorState channels (§4.3).

**Downstream contract the Supervisor consumes** — now a typed model, not a
bare dict (full definition in §3):

```python
DownstreamResult(
    status: DownstreamStatus,            # GROUNDED | UNGROUNDED | ERROR
    response: str,
    customer_wants_escalation: bool,
    schema_version: int = 1,
)
```

- `status == UNGROUNDED` and `customer_wants_escalation is True`
  → reroute to Ticket Agent as `ESCALATION`.
- `status == ERROR` → Supervisor renders a safe fallback string; never
  propagates an exception to the API layer.
- Anything else → FINALIZE: pass `downstream_result.response` through.

### 2.2 Knowledge Agent — `agents/knowledge/`

**Job:** hybrid RAG retrieval + citation-grounded answer generation. Unchanged
internals; the only change is what wraps it.

**Graph topology** (`graph.py`):

```
rewrite -> extract -> [decide_strategy: structured | vector | hybrid]
  -> structured_lookup / vector_search (parallel in hybrid)
  -> rank -> deduplicate -> build_context -> build_prompt -> llm -> END
```

**Public API** — populate `__init__.py` to re-export
`build_knowledge_agent_graph`, `KnowledgeAgentConfig`, `GroundedResponse`.

**Output contract** (`types.py:GroundedResponse`):
```python
GroundedResponse(answer_text: str, is_grounded_hint: bool, citations: tuple[ChunkProvenance, ...])
```
(`is_grounded_hint` — the Knowledge Agent may emit its own heuristic, but the
authoritative groundedness call is Safety's, run as a separate node. Knowledge
never emits a `DownstreamResult` itself.)

**Integration implications (revised):**
- **Adapter is async**; it closes over `config`, the three LLM completions,
  `session_factory`, and `embed_query`, and is invoked with a bounded timeout
  (§4.6) rather than an unbounded `await`.
- `conversation_history` is rendered with **role labels preserved**
  (`"User: ..."` / `"Assistant: ..."`), not bare content strings — see §5.
- Knowledge's adapter node returns only `GroundedResponse`-shaped state into
  `SupervisorState`; it does **not** call Safety itself and does **not**
  construct `DownstreamResult`. That happens in the separate `safety_gate`
  node (§2.4), keeping retrieval/generation and the safety check independently
  testable and traceable.

### 2.3 Ticket Agent — `agents/ticket_agent/`

**Job:** turn a query + validated email into an immutable, idempotently
created `Ticket`.

**Flow** (`ticket_agent.py`):
1. `call(query)` → `PendingTicket(query)` — ticket opened, no email yet.
2. `create_ticket(pending, email, idempotency_key)` →
   `Ticket(ticket_id=uuid4, email, query, priority=None)` — validates email
   via `validation.validate_email` (syntax-only) and de-dupes on
   `idempotency_key` so a retried request can't create a duplicate ticket.
3. `open_ticket(query, email)` — convenience composing both steps.

**Output contract** (`types.py`):
```python
Ticket(ticket_id: str, email: str, query: str, priority: str | None = None)
```

**Persistence — resolved:** the adapter node persists via the `Ticket` table
(`db/models.py:318`) directly after `create_ticket` succeeds, inside the same
async unit of work. Keeping persistence in the adapter (not a separate
service-level step) means a graph-level test can assert both the returned
`DownstreamResult` and the DB row in one pass, and there's no window where the
graph reports success but persistence hasn't happened yet.

**Integration implications (revised):**
- The adapter renders `Ticket` into a confirmation string
  (`downstream_result.response`), e.g. `"Ticket #{ticket_id} opened — we'll
  follow up at {email}."`.
- **Multi-turn email collection** no longer needs a `pending_ticket` field on
  `SupervisorState`. The adapter calls `interrupt()` (LangGraph human-in-the-loop
  primitive) when it has a query but no email yet; the graph pauses, the
  checkpointer persists the paused state keyed by `thread_id`, and the next
  user turn resumes the same node with the email supplied. See §4.3.
- **Idempotency key** = `hash(thread_id, turn_index)` or a client-supplied
  request ID, passed into `create_ticket` so a network retry never double-books
  a ticket.
- `CHECK_TICKET_STATUS` — **scoped out of MVP explicitly** (was previously an
  open question). Routed to a clarification response ("status lookups aren't
  available yet") until a DB-read implementation is prioritized.

### 2.4 Safety Agent — `agents/safety_agent/`

**Job:** independent groundedness gate on the Knowledge Agent's answer, plus a
safe fallback, reported in exactly the shape the Supervisor expects. **Now
wired as its own graph node**, not folded into the Knowledge adapter.

**Public API:**
- `groundedness.check_groundedness(answer, retrieved_chunks, embedding_model,
  sentence_threshold=0.75, aggregation_cutoff=0.80)` → `GroundednessResult`.
- `fallback_response.generate_fallback_response(query)` → templated fallback.
- `report.report_grounded(response_text)` → `DownstreamResult(status=GROUNDED, ...)`.
- `report.report_ungrounded(query, result, customer_wants_escalation)` →
  `DownstreamResult(status=UNGROUNDED, ...)`.

`report.py` now returns the typed `DownstreamResult` model from §3, not a raw
dict — this is the one function in the whole system responsible for
constructing that contract, which makes it the single point to audit for
correctness.

**Integration implications (revised):**
- Runs as its own `safety_gate` node, positioned after `knowledge_agent` and
  before the `decide_post_downstream` conditional edge.
- Takes `retrieved_chunks` from `KnowledgeAgentState` and the shared
  `embedding_model` instance (same BGE model injected everywhere else — one
  singleton, not re-loaded per call).
- Two-step ungrounded escalation (`customer_wants_escalation=False` then
  `True`) uses the **same checkpointer/interrupt mechanism** as ticket email
  collection (§4.3) — there is now exactly one multi-turn pattern in the
  codebase, not two different ones.

---

## 3. Shared contracts (frozen before any wiring work — Phase 0)

New module: `agents/contracts.py`, imported by Supervisor, Knowledge, Ticket,
and Safety. No package constructs `downstream_result` as a bare dict anywhere.

```python
from enum import Enum
from typing import Literal, TypedDict
from pydantic import BaseModel

class DownstreamStatus(str, Enum):
    GROUNDED = "GROUNDED"
    UNGROUNDED = "UNGROUNDED"
    ERROR = "ERROR"

class DownstreamResult(BaseModel):
    status: DownstreamStatus
    response: str
    customer_wants_escalation: bool = False
    schema_version: int = 1
    # Observability metadata — logged, never rendered to the customer.
    agent_name: str | None = None
    latency_ms: float | None = None
    citations: tuple[str, ...] = ()
    confidence: float | None = None

class ConversationTurn(TypedDict):
    role: Literal["user", "assistant"]
    content: str

def flatten_history(history: list[ConversationTurn]) -> tuple[str, ...]:
    """Render history for agents that need a flat sequence, preserving role."""
    return tuple(f"{t['role'].capitalize()}: {t['content']}" for t in history)
```

**Why `TypedDict`, not `BaseModel`, for `ConversationTurn`:** the existing
callers in `supervisor/node.py` and `supervisor/llm_client.py` already access
turns via `turn['role']` / `turn['content']` subscript syntax, matching the
`TypedDict` that `supervisor/schema.py` previously defined locally.
`schema.py` now re-exports `ConversationTurn` from `contracts.py` instead of
defining its own — same type, one definition, zero changes required in
`node.py`/`llm_client.py`. `DownstreamResult` stays a Pydantic `BaseModel`
since nothing depends on dict-style access there and validation is genuinely
useful on that boundary.

Freezing this before Phase 1 means Knowledge, Ticket, and Safety can all be
implemented/tested against the same types independently, without waiting on
each other or on Supervisor's graph wiring.

---

## 4. Integration plan

Design decision (unchanged from v1): keep each agent's compiled LangGraph as
the single source of truth for its own internals, and add a thin **adapter
node** inside the Supervisor graph for each downstream agent. What changes in
this revision is *what* goes into those adapters and *when* each piece of
groundwork happens.

### Phase 0 — Foundations (blocks everything else) — ✅ complete

1. **Done.** Landed `agents/contracts.py` (§3) — `DownstreamResult`,
   `DownstreamStatus`, `ConversationTurn` (as `TypedDict`, re-exported by
   `supervisor/schema.py` in place of its former local definition),
   `flatten_history`. Covered by 9 new tests in
   `tests/agents/contracts/test_contracts.py`.
2. **Done.** Resolved composition style (was open question #1) as *optional
   injection params*, not a full rewrite: `build_supervisor_graph(...)` now
   accepts `knowledge_graph`, `ticket_ops`, `safety_check`, and `checkpointer`
   as optional keyword args, each defaulting to `None` and falling back to
   current placeholder behavior when unset. This keeps every existing test
   green and means Phases 1-4 each flip exactly one param from `None` to a
   real value — no further signature changes needed. Covered by graph tests
   asserting injected-node override (knowledge + ticket) and
   placeholder-when-not-injected.
3. **Done, scoped deliberately.** Checkpointer is `langgraph.checkpoint.memory.MemorySaver`
   for this phase — zero new dependencies, proves out the `thread_id`-keyed
   interrupt/resume abstraction that Phases 2 and 4 build on. **Durable
   backend selection (SQLite vs. Postgres) is explicitly deferred to Phase 5**,
   once real deployment topology (single- vs. multi-instance) is known —
   swapping the backend is a one-line change at the `build_supervisor_graph(checkpointer=...)`
   call site as long as the abstraction boundary is respected. Covered by a
   thread-scoped `MemorySaver` checkpointer test.
4. **Not yet done** — `trace_id` generation at the API boundary, threaded
   through `SupervisorState`, is still open. Carried into Phase 5 alongside
   `api/routes.py` (§4.5), since there's no API boundary to generate it at
   until then; the field itself can be added to `SupervisorState` earlier if
   Phase 1/2 wiring wants it for span correlation sooner.

**Implementation note — pre-existing test collection bug, fixed in this
phase:** the supervisor test suite (`tests/agents/supervisor_agent_test/`,
formerly `tests/supervisor_agent_test/`) was silently never running before
Phase 0, for two independent reasons unrelated to the contracts refactor: (a)
the file was named `test-supervisor.py` (hyphenated), which doesn't match
pytest's default `python_files` patterns; (b) it imported via
`ai_customer_assistant.agents.supervisor...` while the project's `pythonpath`
config only resolves top-level `agents.*`, unlike every other agent test.
Fixed by renaming to `test_supervisor.py` and switching imports to
`agents.supervisor.*`, matching the convention every other agent test already
follows. This surfaced 18 previously-uncollected tests (146 → 176 passed).
Confirmed via `git stash` that this predates the contracts work.

### Phase 1 — Wire the Knowledge Agent ✅ complete
1. **Done.** Populated `agents/knowledge/__init__.py`: re-exports
   `build_knowledge_agent_graph`, `KnowledgeAgentConfig`, `KnowledgeAgentState`,
   `GroundedResponse`, `EmbeddingFunction` — the public API surface.
2. **Done.** Added `make_knowledge_agent_node(knowledge_graph, timeout_s=8)` in
   `agents/supervisor/agents_wiring.py`:
   - maps `SupervisorState.user_message` → `raw_query`, and `conversation_history`
     → flattened role-labeled strings via `flatten_history` (matching Knowledge's
     `conversation_history: tuple[str, ...]` channel);
   - calls `knowledge_graph.ainvoke(...)` under `asyncio.wait_for(..., timeout_s)`;
   - on timeout / exception / missing `response`, returns a `GroundedResponse`-shaped
     **error marker** (`knowledge_response["error"]` set) — never a `DownstreamResult`,
     which remains Safety's job downstream (Phase 2);
   - success returns only `GroundedResponse` fields under `knowledge_response`:
     `{"answer_text", "is_grounded", "citations"}` — the new `knowledge_response`
     channel on `SupervisorState`.
3. **Done.** `build_supervisor_graph` now accepts an optional `knowledge_graph`
   (plus `knowledge_timeout_s`), wrapping it via `make_knowledge_agent_node` to
   replace the Knowledge placeholder. An explicit `knowledge_agent_node` still
   takes precedence (hand-rolled fakes in tests). **Async caveat confirmed live
   (open question #5):** once a real knowledge graph is wired, the compiled
   Supervisor graph is async and callers must `await graph.ainvoke(...)` — verified
   end-to-end in the wiring test.
4. **Done.** Tests in `tests/agents/supervisor_agent_test/test_agents_wiring.py`
   (10): success mapping (raw_query, flattened history with role labels, empty
   history, citations), timeout / exception / missing-response error markers, and
   graph-wiring (`knowledge_graph` param builds; `ainvoke` end-to-end populates
   `knowledge_response`).

**Implementation notes — Phase 1:**
- `pytest-asyncio` was declared in dev deps but not installed in the venv; added
  so the async wiring test can run (`uv pip install pytest-asyncio`). No
  pyproject.toml change required — it was already listed as an optional dev
  dependency.
- The merged code's injection seam is `knowledge_agent_node`, not the plan's
  literal `knowledge_graph` param; both are supported, but **the ambiguity is
  resolved, not deferred** — passing both raises `ValueError` rather than
  letting the callable silently win, so a wired `knowledge_graph` can never be
  silently overridden. Covered by a dedicated test
  (`test_build_graph_rejects_both_knowledge_sources`).
- `build_supervisor_graph` remains usable with `.invoke()` as long as no
  `knowledge_graph` is passed (all pre-existing tests unaffected).

### Phase 2 — Wire the Safety Agent as its own node
1. Add `safety_gate` as a distinct node (not folded into Knowledge's adapter),
   positioned right after `knowledge_agent` in the graph.
2. It calls `check_groundedness(...)` against the Knowledge node's output,
   then `report_grounded` / `report_ungrounded`, producing the canonical
   `DownstreamResult`.
3. If the Knowledge node signaled an upstream error (from Phase 1 step 2),
   `safety_gate` short-circuits to `DownstreamResult(status=ERROR, response=<fallback>)`
   rather than attempting a groundedness check on a failed retrieval.
4. Escalation confirmation (`customer_wants_escalation=False` → ask → `True`)
   uses `interrupt()` against the Phase 0 checkpointer — no new state fields.

### Phase 3 — Decouple routing from assembly, then fix the escalation edge
1. Extract `decide_post_downstream` into its own conditional edge, evaluated
   right after `safety_gate` / `ticket_agent`, so `assemble_response` is
   reduced to pure string formatting.
2. Add the conditional edge's `ESCALATE` branch pointing at the `ticket_agent`
   node (re-entrant), and its `FINALIZE` branch pointing at `assemble_response`.
3. Test: `UNGROUNDED` + `customer_wants_escalation=True` reaches `ticket_agent`;
   `UNGROUNDED` + `False` reaches `assemble_response` with the fallback text.

### Phase 4 — Wire the Ticket Agent
1. Add `make_ticket_agent_node(ticket_ops, session_factory)`:
   - on the opening turn, calls `call(query)`, then `interrupt()`s for the
     email (checkpointer persists the paused state — no `pending_ticket`
     field needed);
   - on resume, calls `create_ticket(pending, email, idempotency_key)` and
     persists via the `Ticket` table in the same unit of work;
   - renders the confirmation string and returns
     `DownstreamResult(status=GROUNDED, response=<confirmation>)`.
2. Route `CHECK_TICKET_STATUS` to a clarification/out-of-scope response
   (explicitly scoped out of MVP — resolved, not left open).
3. Tests: opening turn produces an interrupt; resumed turn with a valid email
   produces a persisted `Ticket` row and correct `DownstreamResult`; a
   duplicate resume with the same idempotency key does not create a second row.

### Phase 5 — Serving layer
- `services/chat_service.py` owns dependency construction: build the
  checkpointer, the three compiled subgraphs, the shared embedding model
  singleton, then `build_supervisor_graph(knowledge_graph, ticket_ops,
  safety_check, checkpointer=...)`. Exposes
  `async def handle_message(thread_id, user_message) -> str`.
- History is **not** passed in by the caller on every request — it's read
  from the checkpointer by `thread_id`. The API only ever sends the new
  `user_message` plus the `thread_id`. (This replaces the v1 plan's
  "service passes accumulated conversation_history into each invoke.")
- `api/routes.py` exposes the chat endpoint, generates/propagates `trace_id`.
- **Select and migrate to a durable checkpointer backend** (SQLite for
  single-instance deployments, Postgres if multi-instance or already in the
  stack), informed by actual deployment topology. Replaces the `MemorySaver`
  used through Phases 0-4 — this is the deferred decision noted in Phase 0
  step 3; it does not happen automatically, it's a deliberate swap at the
  `build_supervisor_graph(checkpointer=...)` call site.
- `main.py` replaced with the FastAPI app bootstrap, including checkpointer
  and embedding-model singleton lifecycle (startup/shutdown hooks).

### Phase 6 — Dependency implementation
- Implement real provider callables for Knowledge's `rewrite_llm_complete`,
  `extraction_llm_complete`, `answer_llm_complete`, copying the pattern from
  `supervisor/llm_client.py` (Gemini/Groq clients). ✅ Landed —
  `agents/knowledge/providers.py` (Anthropic + Groq + Gemini + deterministic
  stub, mirroring the Supervisor resolver's explicit-arg > config > stub
  fallback incl. missing-credential degradation).
- `embed_query` and Safety's `embedding_model` share one BGE
  (`SentenceTransformer`) instance, constructed once in `chat_service` and
  injected everywhere — never re-instantiated per request. ✅ Landed —
  `services/embeddings.py` (`build_shared_embeddings`, module-level cache)
  + `services/chat_service.py` (`_build_real_knowledge_graph` wiring; the
  same instance also surfaces as `ChatService.embedding_model` for Safety's
  binding). Tests: `tests/services/test_embeddings.py`.

### Testing strategy
- Every node remains testable via `make_*_node(...)` factory closures with
  fakes, per the existing convention.
- Contract tests: assert every producer of `DownstreamResult` (Safety's
  `report.py`, Ticket's adapter, the timeout/error paths) validates against
  the shared Pydantic model — this catches drift automatically instead of
  relying on manual review.
- Checkpointer tests: interrupt → resume flows for both ticket email
  collection and escalation confirmation, including a "resume with garbage
  input" case.
- Idempotency test: duplicate `create_ticket` calls with the same key produce
  one row.
- Graph-level integration tests for all three hand-offs: Supervisor→Knowledge→Safety
  (grounded, ungrounded, and error paths), Supervisor→Ticket (open + resume),
  and the escalation reroute.
- Load/latency smoke test on the async boundary (Phase 1) — confirm
  `ainvoke` end-to-end latency stays within the adapter's timeout budget.

---

## 5. Conversation history handling

- **Canonical representation everywhere:** `list[ConversationTurn]` (role +
  content), defined once in `contracts.py`. Any agent needing a flat sequence
  uses `flatten_history()`, which preserves role labels (`"User: ..."` /
  `"Assistant: ..."`) rather than emitting bare content strings.
- **Ownership moves server-side.** History lives behind the Phase 0
  checkpointer, keyed by `thread_id`. The client sends only the new message
  and the thread ID — it does not resend the full transcript every call. This
  removes unbounded per-request payload growth and makes the backend the
  single source of truth for conversation state (also required for the
  interrupt/resume multi-turn flows in §4.3/§4.4).
- **Windowing/summarization:** once a thread's history exceeds a configured
  token budget, a summarization step compresses older turns before they reach
  the Knowledge Agent's rewrite/extraction prompts, so long conversations
  don't silently blow past context limits or inflate cost. (Not present in
  v1 — added here as a required Phase 5 item, not a nice-to-have.)

---

## 6. `downstream_result` handling

- Always the typed `DownstreamResult` model from §3 — never a bare dict.
  `status` is a `DownstreamStatus` enum, not a magic string.
- `ERROR` is first-class: any adapter failure (timeout, DB error, LLM
  exception) resolves to a `DownstreamResult(status=ERROR, response=<safe
  fallback>)`, so `assemble_response` never has to handle a raw exception and
  the API layer never sees one either.
- Observability fields (`agent_name`, `latency_ms`, `citations`, `confidence`)
  are populated by every producer and logged with `trace_id`, but never
  rendered to the customer — keeps the customer-facing contract minimal while
  giving full debuggability.
- `schema_version` on the model means a future field addition doesn't
  silently break a consumer pinned to an older shape.

---

## 7. Open questions — resolved vs. remaining

| # | Question (v1) | Resolution in this revision |
|---|---|---|
| 1 | Graph composition style | **Resolved:** `build_supervisor_graph` takes optional `knowledge_graph`/`ticket_ops`/`safety_check`/`checkpointer` params, `None`-defaulted to current placeholder behavior (§4.0.2). Landed in Phase 0. |
| 2 | Ticket persistence location | **Resolved:** in the adapter node, same unit of work as `create_ticket` (§2.3). |
| 3 | `CHECK_TICKET_STATUS` scope | **Resolved:** explicitly out of MVP, routed to clarification (§2.3, §4.4). |
| 4 | Ungrounded escalation UX (two-turn shape) | **Resolved:** implemented via `interrupt()` + checkpointer, same mechanism as ticket email collection (§4.2). |
| 5 | Async boundary acceptability | **Resolved, with a caveat spelled out:** once any node is async, all callers of the compiled graph must use `ainvoke`; this is called out explicitly in Phase 1/5, not left as a review footnote. |
| 6 | Checkpointer backend for production | **Resolved for now, deferred by design:** `MemorySaver` through Phases 0-4 (landed in Phase 0); durable backend (SQLite/Postgres) selected and migrated in Phase 5 once deployment topology is known (§4.5). Not an open question anymore — it's a scheduled decision. |
| 7 | `ConversationTurn` — single definition vs. duplicate in `schema.py` | **Resolved:** `TypedDict` defined once in `contracts.py`; `supervisor/schema.py` re-exports it, its former local `TypedDict` removed. Landed in Phase 0. |
| 8 *(new)* | Idempotency key source (client-supplied request ID vs. server-derived) | **Open** — affects whether the API contract needs a new client-facing field. Needed before Phase 4. |

---

## 8. Files touched (when approved)

| File | Change | Status |
|---|---|---|
| `agents/contracts.py` *(new)* | `DownstreamResult`, `DownstreamStatus`, `ConversationTurn` (`TypedDict`), `flatten_history` | ✅ Landed (Phase 0) |
| `agents/supervisor/schema.py` | Re-export `ConversationTurn` from `contracts.py`, remove local `TypedDict`; **no** `pending_ticket`/`awaiting_escalation_confirmation` fields; `trace_id` still pending | ✅ Re-export landed · ⏳ `trace_id` in Phase 5 |
| `agents/supervisor/graph.py` | Optional `knowledge_graph`/`ticket_ops`/`safety_check`/`checkpointer` params (`None`-defaulted); `MemorySaver` wired | ✅ Landed (Phase 0) |
| `tests/agents/contracts/test_contracts.py` *(new)* | 9 tests: `DownstreamResult` validation, `ConversationTurn` role constraint, `flatten_history` | ✅ Landed (Phase 0) |
| `tests/agents/supervisor_agent_test/test_supervisor.py` *(renamed)* | Renamed from hyphenated `test-supervisor.py`; imports fixed to `agents.supervisor.*` — fixes pre-existing silent test-collection failure | ✅ Landed (Phase 0) |
| `agents/knowledge/__init__.py` | Add public API exports | ✅ Landed (Phase 1) |
| `agents/supervisor/agents_wiring.py` *(new)* | Knowledge, Safety, Ticket adapter nodes (each single-responsibility) | ✅ Knowledge adapter landed (Phase 1) · ⏳ Safety/Ticket in Phases 2, 4 |
| `agents/supervisor/graph.py` (further changes) | Replace placeholder nodes with real ones; add `safety_gate` node; extract `decide_post_downstream` into a conditional edge; add escalation edge | ✅ `knowledge_graph` wiring (Phase 1) · ⏳ `safety_gate`/edges in Phases 2-3 |
| `agents/supervisor/llm_client.py` | Optional: shared provider helpers for Knowledge LLM completions | ⏳ Phase 6 |
| `agents/safety_agent/report.py` | Return `DownstreamResult` (typed), not a dict | ⏳ Phase 2 |
| `agents/ticket_agent/ticket_agent.py` | Add idempotency key handling | ⏳ Phase 4 |
| `services/chat_service.py` | Dependency construction, checkpointer wiring, `handle_message(thread_id, user_message)` | ✅ Landed (Phase 5) · ⏳ real knowledge-graph build wired via `shared_embeddings`+`session_factory` (Phase 6) |
| `services/embeddings.py` *(new)* | `SharedEmbeddings` + `build_shared_embeddings`: one BGE instance feeding `embed_query` and `embedding_model` | ✅ Landed (Phase 6) |
| `db/session.py` (further changes) | Async engine/factory (`get_async_engine`, `get_async_session_factory`, `get_async_session`) for the Knowledge Agent's retrieval nodes | ✅ Landed (Phase 6) |
| `agents/knowledge/providers.py` *(new)* | Real Knowledge LLM providers (Anthropic/Groq/Gemini + stub) + `llm_completions` | ✅ Landed (Phase 6) |
| `api/routes.py` | Chat endpoint; `trace_id` generation | ⏳ Phase 5 |
| `main.py` | FastAPI app bootstrap incl. checkpointer + embedding-model singleton lifecycle | ⏳ Phase 5 |
| `tests/...` (remaining) | Adapter + graph-level integration tests for Phases 1-4; idempotency tests | ✅ `test_agents_wiring.py` (Phase 1) · ⏳ Safety/Ticket in Phases 2-4 |


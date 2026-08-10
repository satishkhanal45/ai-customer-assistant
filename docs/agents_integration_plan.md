# Agent Integration Plan — Supervisor · Knowledge · Ticket · Safety

Status: **planning only — no code changed.**
Scope: `backend/src/ai_customer_assistant/agents/` plus the layers that will
eventually call it (`services/chat_service.py`, `api/routes.py`, `main.py`).

This document records (1) what each agent does today and the contract it
exposes, and (2) a step-by-step plan to wire them into one end-to-end graph.

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

## 2. Work of each agent

### 2.1 Supervisor Agent — `agents/supervisor/`

**Job:** domain gate, intent classification, and routing. It never retrieves
knowledge and never creates tickets itself; it decides *which* downstream agent
handles a turn.

**Public API** (`__init__.py`):
- `build_supervisor_graph(llm_client=None)` — builds/compiles the top-level LangGraph.
- `SupervisorLLMClient` protocol + `StubSupervisorLLMClient`, `GeminiSupervisorLLMClient`,
  `GroqSupervisorLLMClient`, and `build_llm_client(provider)` (`llm_client.py`).
- `Classification`, `parse_llm_response` (`classification.py`).
- `RoutingDecision`, `decide_route`, `decide_post_downstream` (`routing.py`).
- Types: `SupervisorState`, `Intent`, `NextAgent`, `RequestCategory`, `TicketType`
  (`schema.py`).

**Graph topology** (`graph.py`):

```
classify_and_route
   └─ conditional edge
      ├─ END                     (GREETING / OUT_OF_SCOPE / clarification question)
      ├─ knowledge_agent node
      └─ ticket_agent node
knowledge_agent / ticket_agent ──> assemble_response ──> END
```

**State contract** (`schema.py:SupervisorState`):
- Inputs: `user_message`, `conversation_history` (`list[ConversationTurn]` with
  `role`/`content`), `downstream_result`.
- Supervisor-owned: `request_category`, `domain_confidence`, `intent`,
  `intent_confidence`, `clarification_required`, `clarification_question`,
  `clarification_attempts`, `next_agent`, `ticket_type`, `final_response`.

**Downstream contract the Supervisor already expects**
(`routing.py:decide_post_downstream`, `report.py` cross-checks it):

```python
downstream_result = {
    "status": "UNGROUNDED" | <anything else>,   # only "UNGROUNDED" is special
    "response": <str>,
    "customer_wants_escalation": <bool>,
}
```

- `status == "UNGROUNDED"` and `customer_wants_escalation is True`
  → reroute to Ticket Agent as `ESCALATION`.
- Anything else → FINALIZE: pass `downstream_result["response"]` through.

**Notable gaps found:**
- The post-downstream escalation branch is decided in `assemble_response_node`
  (`node.py:57`) but the graph has **no edge from `assemble_response` back to the
  ticket node** (`graph.py:77` is `assemble_response -> END` only). Escalation is
  computed but not traversable.
- Routing also handles `CHECK_TICKET_STATUS` → Ticket Agent, but Ticket Agent has
  no status-lookup implementation yet (see 2.3).

### 2.2 Knowledge Agent — `agents/knowledge/`

**Job:** hybrid RAG retrieval + citation-grounded answer generation.

**Graph topology** (`graph.py`):

```
rewrite -> extract -> [decide_strategy: structured | vector | hybrid]
  -> structured_lookup / vector_search (parallel in hybrid)
  -> rank -> deduplicate -> build_context -> build_prompt -> llm -> END
```

**State** (`state.py:KnowledgeAgentState`): `raw_query`,
`conversation_history` (`tuple[str, ...]`), then a chain of stage outputs ending
in `response: GroundedResponse`.

**Public API** — `__init__.py` is **empty**; the real entry point is
`graph.py:build_knowledge_agent_graph(...)` which takes **all dependencies via
explicit injection**:

```python
build_knowledge_agent_graph(
    config: KnowledgeAgentConfig,          # pydantic-settings, KNOWLEDGE_AGENT_* env
    rewrite_llm_complete:  Callable[[str], str],
    extraction_llm_complete: Callable[[str], str],
    answer_llm_complete:   Callable[[str, str], str],
    session_factory: async_sessionmaker[AsyncSession],
    embed_query: EmbeddingFunction,
) -> CompiledStateGraph
```

**Output contract** (`types.py:GroundedResponse`):
```python
GroundedResponse(answer_text: str, is_grounded: bool, citations: tuple[ChunkProvenance, ...])
```

Invocation shape: `graph.ainvoke({"raw_query": ..., "conversation_history": (...)})`
returns state with a populated `response` field.

**Integration implications:**
- The Knowledge Agent is async-first (`ainvoke`, `session_factory`); the Supervisor
  graph currently runs synchronous node functions. The adapter node must be async
  (LangGraph supports `async def` nodes) and needs a DB `session_factory` plus an
  embedding function injected at construction time — the Supervisor's
  `build_supervisor_graph` signature must grow to accept them (or a higher-level
  composer builds the app graph instead).
- `conversation_history` type mismatch: Supervisor carries `list[ConversationTurn]`
  (role+content dicts), Knowledge expects `tuple[str, ...]`. The adapter must map one
  to the other.
- The Knowledge graph does **not** emit the Supervisor's `downstream_result` dict;
  producing that shape is Safety Agent's job (see 2.4).

### 2.3 Ticket Agent — `agents/ticket_agent/`

**Job:** turn a query + validated email into an immutable `Ticket`.

**Flow** (`ticket_agent.py`):
1. `call(query)` → `PendingTicket(query)` — ticket opened, no email yet.
2. `create_ticket(pending, email)` → `Ticket(ticket_id=uuid4, email, query, priority=None)`
   — validates email via `validation.validate_email` (`email-validator`, syntax-only).
3. `open_ticket(query, email)` — convenience composing both steps.

**Output contract** (`types.py`):
```python
Ticket(ticket_id: str, email: str, query: str, priority: str | None = None)
```

**Explicitly out of scope here:** persisting the ticket. The DB already has a
`Ticket` table (`db/models.py:318` — `ticket_id`, `email`, `query`, `priority`,
`status`), so a persistence step is a separate layer the graph must call after
`create_ticket`.

**Integration implications:**
- The Supervisor's `downstream_result["response"]` is a *string*; the Ticket Agent
  returns a *`Ticket` object*. The adapter node must render a confirmation message
  (e.g. ticket number) and optionally persist, then build `downstream_result`.
- **Multi-turn email collection.** `call()` → `PendingTicket` means one turn opens
  the ticket and a later turn supplies the email. The Supervisor graph is stateless
  per-invoke and `SupervisorState` has no "pending ticket" channel. The plan must
  add a field (e.g. `pending_ticket`) and a route back to the ticket node so the
  email turn completes the ticket.
- `CHECK_TICKET_STATUS` routes here but there is **no status-lookup function**.
  Either add one (read `Ticket.status` from the DB) or scope it out explicitly.

### 2.4 Safety Agent — `agents/safety_agent/`

**Job:** independent groundedness gate on the Knowledge Agent's answer, plus a
safe fallback, reported in exactly the shape the Supervisor expects.

**Public API:**
- `groundedness.check_groundedness(answer, retrieved_chunks, embedding_model,
  sentence_threshold=0.75, aggregation_cutoff=0.80)` → `GroundednessResult(is_grounded,
  confidence_score, sentence_scores)`. Per-sentence embedding similarity using the
  existing BGE `SentenceTransformer`.
- `fallback_response.generate_fallback_response(query)` → templated fallback message.
- `report.report_grounded(response_text)` → `{"status": "GROUNDED", "response": ..., "customer_wants_escalation": False}`.
- `report.report_ungrounded(query, result, customer_wants_escalation=False)` →
  `{"status": "UNGROUNDED", "response": <fallback>, "customer_wants_escalation": <bool>}`.

`report.py:20-26` explicitly notes it builds the dict but that "wiring them into an
actual Knowledge Agent node ... is a separate piece, out of Safety Agent's scope."

**Integration implications:**
- Safety runs *after* the Knowledge graph's `llm` node and *before* the Supervisor's
  `assemble_response`. It needs the retrieved chunks + embeddings (already in
  `KnowledgeAgentState`: `retrieved_chunks`; embeddings come from the DB) and the
  `embedding_model` (injected — same model family the pipeline already loads).
- It resolves the "who reports to the Supervisor" question: the Knowledge adapter
  node should return Safety's `downstream_result`, not the raw `GroundedResponse`.
- The two-step ungrounded flow (`customer_wants_escalation=False` first, then `True`
  after the customer answers the fallback question) needs the same multi-turn
  machinery as ticket email collection.

---

## 3. Integration plan

Design decision: keep each agent's compiled LangGraph as the single source of
truth for its own internals, and add a thin **adapter node** inside the Supervisor
graph for each downstream agent. The Supervisor graph stays the orchestrator; the
adapters are the only place that maps between `SupervisorState` and each agent's
own state/contract.

### Phase 0 — Contract alignment (no new agents)
- Document and freeze `downstream_result` as the canonical hand-off dict
  (already implemented in `safety_agent/report.py`; Supervisor `decide_post_downstream`
  already consumes it).
- Decide the `conversation_history` representation: keep Supervisor's
  `list[ConversationTurn]`, and have the adapter render it to
  `tuple[str, ...]` for the Knowledge graph (or add a helper `supervisor/schema.py`
  → `flatten_history()`).

### Phase 1 — Wire the Knowledge Agent into the Supervisor graph
1. Populate `agents/knowledge/__init__.py` with a public API (at minimum re-export
   `build_knowledge_agent_graph`, `KnowledgeAgentConfig`, `GroundedResponse`).
2. Add a `make_knowledge_agent_node(...)` adapter in a new module
   (e.g. `agents/supervisor/agents_wiring.py`) that:
   - closes over `config`, the three LLM completions, `session_factory`, `embed_query`;
   - maps `SupervisorState.user_message`/`conversation_history` → Knowledge state input;
   - calls `knowledge_graph.ainvoke(...)`;
   - maps the resulting `GroundedResponse` → `downstream_result` **via Safety Agent**
     (see Phase 2);
   - returns only Supervisor-owned fields (e.g. `downstream_result`).
3. Replace `graph.py:69` `_placeholder_agent_node("Knowledge Agent")` with the real
   node. Decide injection strategy:
   - **Option A (recommended):** extend `build_supervisor_graph(...)` to accept the
     knowledge dependencies (config, LLM completions, session_factory, embed_query)
     so everything is injectable and testable with fakes, mirroring the existing
     `llm_client` pattern.
   - **Option B:** build a higher-level `build_app_graph(...)`/`chat_service` that
     composes both subgraphs and injects the wiring node; Supervisor stays unchanged.
4. Add tests mirroring the existing `tests/agents/supervisor_agent_test/test-supervisor.py`
   pattern, asserting `downstream_result` shape for grounded answers.

### Phase 2 — Wire the Safety Agent (groundedness gate)
1. Add a node (or fold into the Knowledge adapter) that runs
   `check_groundedness(...)` on the Knowledge graph's answer against its retrieved
   chunks, then calls `report_grounded(...)` / `report_ungrounded(...)`.
2. Confirm the escalation signal: when `is_grounded=False` and (per design) the
   customer has not yet been asked, emit `customer_wants_escalation=False`; after the
   customer confirms, re-emit with `True` so `decide_post_downstream` reroutes to the
   Ticket Agent.
3. Note: with `is_grounded=False`, still hand the (fallback) `response` through so
   the customer sees a message and the graph never dead-ends.

### Phase 3 — Fix the escalation edge in the Supervisor graph
1. Add `graph.add_edge(ASSEMBLE_NODE, TICKET_AGENT_NODE)` (or a conditional edge on
   `next_agent`) so the `ESCALATE` branch of `decide_post_downstream` actually loops
   back into the Ticket node instead of falling through to `END` (`graph.py:77`).
2. Add a test for the post-downstream escalation path (UNGROUNDED +
   `customer_wants_escalation=True` → Ticket node reached).

### Phase 4 — Wire the Ticket Agent
1. Replace `graph.py:70` `_placeholder_agent_node("Ticket Agent")` with a
   `make_ticket_agent_node(...)` adapter that:
   - calls `call(query)` → `PendingTicket` on the opening turn;
   - on the follow-up turn (email supplied) calls `create_ticket(...)`;
   - **renders a confirmation string** (Supervisor's `downstream_result["response"]`
     must be text) and, if persistence is in scope, saves via the `Ticket` table
     (`db/models.py:318`);
   - returns `{"downstream_result": {...}}`.
2. Add state to `SupervisorState` for the multi-turn flows:
   - `pending_ticket: Optional[dict]` (query awaiting email),
   - `awaiting_escalation_confirmation: bool` (for the ungrounded follow-up),
   and add edges/conditional routing so a second turn reaches the same node.
3. Scope decision required: implement `CHECK_TICKET_STATUS` (a DB read) or explicitly
   route it to clarification/out-of-scope for MVP.

### Phase 5 — Serving layer (completes the loop)
- `services/chat_service.py` (empty) becomes the seam that owns dependency
  construction: build `KnowledgeAgentConfig`, LLM completions, `session_factory`,
  embedding model, then `build_supervisor_graph(...)` (or the composed app graph),
  and expose an `async def handle_message(user_message, conversation_history) -> str`.
- `api/routes.py` (empty) exposes the chat endpoint calling `chat_service`.
- `main.py` currently just prints "Hello Customer!"; replace with the FastAPI app.
- Keep the multi-turn state out of the graph where possible: the *service* should
  pass accumulated `conversation_history` (or explicit `pending_ticket`/confirmation
  flags) into each invoke, rather than the graph holding mutable memory.

### Phase 6 — Dependency implementation (needed before real end-to-end)
- Knowledge Agent's three `LLMCompletion` callables and the `answer_llm_complete`
  `(system, prompt)` shape have **no real provider implementations yet**; Supervisor
  already has Gemini/Groq clients to copy the pattern from (`supervisor/llm_client.py`).
- `embed_query` and the safety `embedding_model` reuse the BGE model from
  `ingestion/chunk_embed/embedding.py` — inject a shared instance.

### Testing strategy
- Keep the existing convention: every node is testable with fakes via
  `make_*_node(...)` factory closures (Knowledge `nodes.py` pattern).
- Unit-test each adapter (state mapping in/out) with a fake downstream graph.
- Graph-level tests assert `next_agent` / `downstream_result` transitions, matching
  `tests/agents/supervisor_agent_test/test-supervisor.py`.
- Add end-to-end contract tests for the three hand-offs: Supervisor→Knowledge→Safety,
  Supervisor→Ticket (open + email turns), and the escalation loop.

---

## 4. Open questions for the team

1. **Graph composition style** — Option A (extend `build_supervisor_graph`) vs
   Option B (compose an app-level graph in `chat_service`)? Recommended: A for
   testability, keeping one entry point.
2. **Ticket persistence** — where does `Ticket` from `ticket_agent/types.py` get
   written to the `Ticket` table (`db/models.py:318`)? In the adapter node, or in a
   service-level step after the graph returns?
3. **`CHECK_TICKET_STATUS`** — implement a DB status read in the ticket agent, or
   explicitly scope it out of the MVP?
4. **Ungrounded escalation UX** — confirm the two-turn shape: turn 1 asks
   "connect you with support?" (`customer_wants_escalation=False`), turn 2 sets it
   `True` and reroutes to Ticket Agent.
5. **Async boundary** — the Supervisor graph nodes are sync today; the Knowledge
   adapter must be async. Confirm LangGraph's mixed sync/async node support is
   acceptable (it is — LangGraph runs both, but be explicit about it in review).

---

## 5. Files touched (when approved)

| File | Change |
|---|---|
| `agents/knowledge/__init__.py` | Add public API exports |
| `agents/supervisor/graph.py` | Replace both placeholder nodes; add escalation edge; extend builder signature (Option A) |
| `agents/supervisor/schema.py` | Add `pending_ticket` / `awaiting_escalation_confirmation` state channels |
| `agents/supervisor/agents_wiring.py` *(new)* | Knowledge + Ticket adapter nodes, Safety wiring |
| `agents/supervisor/llm_client.py` | Optional: shared provider helpers for Knowledge LLM completions |
| `services/chat_service.py` | Dependency construction + `handle_message` |
| `api/routes.py` | Chat endpoint |
| `main.py` | FastAPI app bootstrap |
| `tests/...` | Adapter + graph-level integration tests |

> No code has been changed. This plan requires explicit approval before any edit.

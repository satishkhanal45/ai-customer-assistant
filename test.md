# Live UI Retrieval Test — Evaluation and Fixes

**Date:** 2026-09-06
**Build:** host dev server (`make uvicorn`, port 8002) against the Docker Postgres/MinIO/Tika stack, with every P0-1/P0-2/P1/P2 fix applied
**Method:** a real Chromium browser driven through `frontend/src/pages/chat.js` — typing into the input and pressing Enter, then reading the rendered DOM (`.msg-bubble`, `.citation`) — correlated against the uvicorn terminal log and per-node timing probes
**Corpus:** 27 live chunks across 9 sources

---

## 1. Headline

**Retrieval is not the problem. Generation latency is.**

Retrieval was measured at **0.14–0.24 s** and returned the correct source every time it was asked. The user-visible failures — a spinner for half a minute, then "Error: Request timed out" — come from the four sequential LLM calls that surround retrieval.

| Measure | Result |
|---|---|
| Questions answered correctly with a citation | **7 / 10** answerable |
| Unanswerable questions correctly refused | **2 / 2** |
| Median turn latency **in the browser** | **35 s** |
| Turns over 30 s | **7 / 12** |
| Turns over 45 s | **4 / 12** |
| Fastest / slowest | 4.1 s / 60.0 s (client abort) |
| HTTP status of every failed turn | **200 OK** |

That last row matters: **every failure returned HTTP 200**, so nothing in an access log, uptime check or load-balancer health metric would ever show a problem.

---

## 2. What the browser actually showed

| # | Question | Time | Outcome |
|---|---|---|---|
| 1 | How much does a backend engineer cost per month? | 4.1 s | ✅ `pricing.pdf` |
| 2 | What are the payment terms? | 36.3 s | ✅ `pricing.pdf` |
| 3 | Does the company support remote or hybrid work? | 59.3 s | ❌ *"Sorry, something went wrong on my end."* |
| 4 | How does an engagement begin? | 47.7 s | ✅ `company_overview.pdf` |
| 5 | What are the core values of the company? | 60.0 s | ❌ **"Error: Request timed out."** (client gave up) |
| 6 | Why is UI and UX important when building an MVP? | 44.2 s | ✅ 4 citations |
| 7 | What skills are required for the senior AI engineer role? | 28.5 s | ✅ cited |
| 8 | Why did Soani Tech change its name? | 12.4 s | ❌ **refused as out-of-scope** |
| 9 | What cloud services do you offer? | 50.1 s | ✅ 2 citations |
| 10 | How much is a security review? | 27.0 s | ✅ `pricing.pdf` |
| 11 | *(unanswerable)* refund policy for cancelled subscriptions | 33.6 s | ✅ correctly refused |
| 12 | *(unanswerable)* how do I reset my password | 27.1 s | ✅ correctly refused |

No JavaScript console errors and no page errors were produced in any run — the frontend itself is behaving correctly.

---

## 3. Where the time goes

Per-node timing of the Knowledge subgraph, three consecutive runs of the same question:

| Node | Run 0 | Run 1 | Run 2 |
|---|---|---|---|
| `rewrite` (LLM) | 0.76 s | 0.52 s | 1.94 s |
| `extract` (LLM) | 1.43 s | 1.01 s | 1.23 s |
| `structured_lookup` (Postgres) | 0.24 s | 0.14 s | 0.18 s |
| `vector_search` (pgvector) | **0.23 s** | **0.14 s** | **0.17 s** |
| `rank` / `deduplicate` / `build_context` / `build_prompt` | 0.00 s | 0.00 s | 0.00 s |
| `llm` (answer generation) | 1.26 s | 2.51 s | **13.63 s** |
| **total** | 3.7 s | 4.2 s | **17.0 s** |

Plus one Supervisor `classify` call per turn, measured at **1.1 s**.

Two conclusions:

1. **Everything except the LLM calls is effectively free.** pgvector retrieval, ranking, dedupe and context assembly together cost under a quarter of a second. The P1-1 and P2-2 work did its job.
2. **Answer generation is the variable cost, and it varies by an order of magnitude** — 1.26 s to 13.63 s for the *same question*. A raw Groq call with a 1 000-token prompt returns in 0.6–0.8 s, so the variance is queueing and output length on the free tier, not the network.

Full chat turns through `ChatService` (checkpointer included, debug logging disabled so it could not distort the measurement): **12.1 s / 58.2 s / 40.5 s** for the same question.

---

## 4. Findings

### ✅ F1 — The timeout ladder budgeted one node, not the whole turn — **FIXED 2026-09-06**

**Evidence.** The server log contains **4 `"error": "timeout"`** and **2 `"error": "error"`** Knowledge-node results. Query 5 hit the browser's 60 s limit; query 3 returned the server's fallback at 59.3 s.

`timeouts.py` (added for P2-4) declares:

```
client 60 s  >  knowledge node 45 s  >  one completion 25 s  >    one call 15 s
```

That is correct for the Knowledge node in isolation — and **incomplete**, because a turn is not just the Knowledge node:

```
turn = classify (up to ~46 s worst case)  +  knowledge node (45 s)  +  checkpoint writes
     = up to ~91 s   >   the client's 60 s
```

So the node can honour its 45 s budget and the customer can *still* see a client-side abort. **This is a real gap in the P2-4 fix**, and only running the real UI exposed it.

**Fixed.** The turn is now the budgeted unit:

```
client 60s
  > turn 52s                     hard ceiling, enforced in ChatService
      = classify        12s
      + knowledge node  37s      derived: whatever is left
      + checkpointing    3s
```

Worst-case server-side turn: **52 s, down from ~76 s.**

1. **`TURN_BUDGET_S`, enforced.** `ChatService.handle_message_turn` wraps the turn in `asyncio.wait_for`. The important part is not that it returns a message — it is that the graph is genuinely **cancelled**. Returning early while the work continued would still burn quota on an undeliverable answer, which is precisely what happened when only the *client* gave up. `test_the_server_actually_stops_working` asserts the cancellation, not just the response.
2. **The node budget is derived, not declared.** `KNOWLEDGE_NODE_TIMEOUT_S = TURN_BUDGET_S - CLASSIFY_BUDGET_S - CHECKPOINT_HEADROOM_S` (45 s → 37 s). Declaring it independently is how the arithmetic drifted out of agreement with the client in the first place. An explicit env override still wins, and the consistency check rejects it if it does not fit.
3. **Classification was the unbounded term, and is now bounded.** Its retry loop was capped by *attempt count* only — 3 × 10 s of socket timeout plus backoff, ~31 s, for a call that measures 1.1 s. It now shares the Knowledge provider's wall-clock helper (moved to `timeouts.sleep_within_budget` so there is one definition rather than two that can drift) under a 12 s `CLASSIFY_BUDGET_S`.
4. **The consistency check now tests the sum.** This is the actual lesson of F1: every rung was individually smaller than the client budget while the total was larger, so a pairwise check passed a broken configuration.

**Verified live.** Booting a real server with `KNOWLEDGE_NODE_TIMEOUT_S=50` — individually well under the client's 60 s, and accepted by the old check — is now refused at startup:

```
ValueError: timeout ladder inverted: the server-side budgets sum to 65.0s
(classify 12.0 + knowledge node 50.0 + checkpointing 3.0) but
TURN_BUDGET_S=52.0. Every rung being individually small enough is not
sufficient — this exact gap is what let a turn run to ~76s under a 60s
client budget.
```

**Not yet re-measured end to end.** Confirming that real turns now finish inside 52 s needs a working Groq quota, and the daily cap was exhausted again by this session's testing. The enforcement, the cancellation and the arithmetic are covered by tests; the *latency distribution* under the new budgets is not, and F7 (streaming) is the structural answer to it regardless.

### ✅ F2 — One 15-second timeout for every LLM call was wrong — **FIXED 2026-09-06**

**Evidence.** The answer call was measured at **13.63 s** — inside `LLM_CALL_TIMEOUT_S = 15` but with almost nothing to spare, while `classify` needs 1.1 s and `rewrite` 0.5 s.

When the answer call does exceed 15 s the SDK raises, the retry costs another 15 s, the 25 s `LLM_RETRY_BUDGET_S` is exhausted, and the node returns `"error": "error"` — the opaque failure seen on query 3.

**The three stages have genuinely different cost profiles and should not share one number.** Classification and rewriting emit a few tokens; answer generation emits paragraphs, and generation time scales with output length.

**Fixed.** `timeouts.py` now declares two stage-specific pairs instead of one shared number:

```python
LLM_SHORT_TIMEOUT_S       = 10.0   # classify / rewrite / extract
LLM_SHORT_RETRY_BUDGET_S  = 22.0
LLM_ANSWER_TIMEOUT_S      = 30.0   # measured worst case 13.6s, with headroom
LLM_ANSWER_RETRY_BUDGET_S = 38.0
```

`GroqKnowledgeProvider._complete` takes the pair per call, so `answer_complete` gets the generous budget while `rewrite_complete` / `extraction_complete` keep the tight one. The Supervisor's classifier uses the short budget — which also shortens its worst case from ~46 s to ~31 s, incidentally narrowing the F1 gap.

`assert_ladder_is_consistent()` was rewritten, because with per-stage budgets a single descending chain no longer describes the system. It now enforces two separate rules: no call may outlive its own retry loop, and **no stage's whole retry budget may reach the Knowledge node's ceiling** — otherwise one slow completion eats the budget the node needs for three of them plus retrieval.

**Two things found while doing this:**

- **The Anthropic provider stored a `timeout` it never passed to the SDK** — the same defect already fixed in the Supervisor's Groq client, in a second place. Those calls had no deadline at all. Now fixed for both its stages.
- **I did not add the `max_tokens` cap** I originally proposed. Re-examined, it trades a *diagnosable* failure for an *undiagnosable* one: a 30 s timeout raises a timeout, whereas a token cap truncates the JSON response mid-object and surfaces as a parse error. The timeout already bounds wall clock, which was the actual goal. Worth revisiting only as a cost control, and then with a `max_tokens` large enough that truncation is impossible in practice.

### ✅ F3 — Failures were swallowed with no logging — **FIXED 2026-09-06**

**Evidence.** `"error": "error"` is the *entire* diagnostic the system produced for query 3. Finding the real cause needed a separate reproduction script.

Two places catch broadly and log nothing:

- `agents/supervisor/node.py:79` — `except Exception: return _transient_error_update(state)`
- `agents/supervisor/agents_wiring.py` — `except Exception: return _error_result("error")`

During this session the same silent path hid a Groq `429` twice, which presented to me exactly as it would to a customer: an unexplained apology.

**Fixed.** Both sites now log the exception type with a full traceback, and both distinguish a rate limit from a defect.

`routing.py` gained three pure helpers — `is_rate_limited(exc)`, `failure_response(exc)`, `failure_reason(exc)`. Detection walks the `__cause__` / `__context__` chain, because a 429 does not arrive bare: the Knowledge stages re-raise it as `LLMGenerationError` with the provider text inside. Matching is on type-name fragments and message text rather than an imported exception class, so it works across groq / anthropic / gemini and survives a vendor renaming its class.

A rate limit now renders as *"I'm handling more requests than I can keep up with at the moment… Please try again in a minute"* and is labelled `"error": "rate_limited"`, instead of being indistinguishable from a crash.

**Verified live**, by forcing each failure through the running server with the new env overrides:

```
# LLM_SHORT_TIMEOUT_S=0.001  -> classification fails
supervisor classification failed (APITimeoutError); returning a transient error to the customer
Traceback (most recent call last):
  ...
groq.APITimeoutError: Request timed out.

# LLM_ANSWER_TIMEOUT_S=0.001 -> the answer stage fails
knowledge agent failed (LLMGenerationError)
Traceback (most recent call last):
  ...
agents.knowledge.exceptions.LLMGenerationError: answer-generation LLM call failed: Request timed out.
```

Both produced **nothing at all** in the log before this change. A node timeout is now logged with the budget it exceeded, which is the single most useful line when investigating latency.

One deliberate restraint: the new log lines carry the exception, **not the customer's message**. P0-4 (full conversation state printed to stdout) is still open, and this should not add to it — `test_the_log_does_not_carry_the_customer_message` locks that in.

### ✅ F4 — Unfiltered structured facts diluted the prompt and caused refusals — **FIXED 2026-09-06**

**Evidence.** For *"What are the core values of the company?"* the extractor produced `entity_type='Company', confidence=0.62`, which routes to **hybrid**. Structured lookup then returned **15 facts, none of them about values**:

```
- Company "Company" uses "Agile Delivery Practices"
- Company "Company" uses "Sprint Planning"
- Company "Company" uses "Daily Collaboration"
- Company "Company" uses "Sprint Review"
- Company "Company" uses "Retrospective"
  ... 10 more of the same shape
```

The retrieved documentation *did* contain the answer (`"core value"` and `"integrity"` both present). But the prompt leads with 678 characters of irrelevant "Structured Facts" before the documentation.

Directly observed consequence — the same query run three times:

| Run | facts | chunks | grounded | answer |
|---|---|---|---|---|
| 0 | 0 | 4 | ✅ true | correct |
| 1 | **15** | 4 | ❌ **false** | *"I'm sorry, I don't have information…"* |
| 2 | 0 | 4 | ✅ true | correct |

**The run that retrieved *more* was the run that failed.** When `attribute` and `relation_type` are both `None`, `structured_lookup` returns everything it knows about the entity, and that dump crowds out the documentation that actually answers the question.

**One more thing the prompt does**, which explains why an irrelevant section is so damaging rather than merely useless — `answer.md` rule 4:

> If Structured Facts and Relevant Documentation conflict, **prefer the Structured Facts** (they come from an exact, deterministic record)

The model is explicitly told to privilege the section that, for an unslotted lookup, is an unfiltered dump.

**Fixed.** A new `agents/knowledge/fact_relevance.py` decides which facts earn a place in the prompt:

- A **slotted** lookup — the customer asked for a specific attribute or relation — passes through untouched. Whatever came back *is* the answer to what was asked.
- A **general** lookup is a dump, and survives only where it overlaps the question. Overlap is computed on the fact's attribute, value, relation and related entity — deliberately **not** its entity type or label, since the lookup already matched the entity. Including it would make every fact "relevant" to any question naming the entity, which is exactly the case that broke.
- It **degrades to keeping everything** when it cannot do better: no query text, or a question made entirely of stopwords. Silently discarding facts because the filter had nothing to work with would be a worse failure than the one it prevents.

Applied where the dump is produced — the `structured_lookup` node and `hybrid_retrieve` — rather than downstream, because `ranking.py` deliberately does not carry the `StructuredQuery` forward and `context_builder.py` deliberately does not re-rank. Both those contracts are stated in their own docstrings and are worth keeping.

**Verified against the live corpus and database:**

| Question | facts retrieved | facts kept |
|---|---|---|
| What are the core values of the company? | 15 | **0** |
| Does the company support remote or hybrid work? | 15 | **0** |
| What delivery methodology does the company use? | 15 | **14** |

The third row is the one that matters as much as the first two: the filter does not simply delete general lookups. When the dump is genuinely on topic it survives almost intact — only `serves → Clients` was dropped, correctly.

**A pleasing interaction with F/P1-6.** When a structured-only strategy's dump is filtered to nothing, the fallback added for P1-6 now runs a semantic search instead. An irrelevant dump used to poison the prompt; it now routes the query to the retrieval path that works. `test_the_compiled_graph_falls_back_when_the_dump_is_filtered_out` drives the real graph to prove it.

**Two things I did not do, and why:**

- **I did not reorder or relabel the prompt sections.** Putting documentation first, or softening rule 4, might help — but its effect can only be judged by measuring answer quality, and the Groq daily quota was exhausted. Changing a prompt on reasoning alone is how the uncalibrated 0.70 threshold got there in the first place. The filter addresses the root cause; the prompt change is a tuning question for when there is budget to measure it.
- **I did not lower `max_structured_facts` from 20.** With the dump filtered, the cap is no longer what stands between the model and the noise, so changing it would be a speculative second knob.

**Not confirmed end to end.** The retrieval-side change is verified on live data, and the causal chain to the refusal is well evidenced (facts=15 → refusal, facts=0 → correct answer, observed three times). But the daily Groq quota was exhausted again, so I could not re-run the generation step and watch those questions answer correctly. That is the one claim here resting on inference rather than observation.

**Still worth doing:** add these questions to `tests/data/golden_retrieval.json`. Note that `scripts/calibrate_retrieval.py` would *not* have caught this — it measures retrieval, and retrieval was correct throughout. Catching it needs an answer-quality metric, not a recall one.

### ✅ F5 — The same question gave different answers on different runs — **FIXED 2026-09-06**

**Evidence.** `extract_query` returned `entity_type='Policy', confidence=0.85` on one run and `entity_type='Company', confidence=0.62` on another, for the same input at `temperature=0`. Those route to **structured** and **hybrid** respectively, which produce different context and different answers (F4).

**Fixed.** Routing now asks one question — *is there an entity to look up?* — and nothing about the shape or confidence of the extraction.

```
hybrid  <- an entity was resolved      (structured + semantic, concurrently)
vector  <- no entity was resolved      (nothing to look up)
```

**`STRATEGY_STRUCTURED` is no longer a routing outcome.** It was the whole problem: an entity plus a slot plus enough confidence meant an exact-fact lookup with *no semantic search at all*, so whether a question got documentation depended on how the extractor happened to phrase itself that run. It is still implemented, because it carries the P1-6 fallback and `hybrid_retrieve` is a public function a caller can drive with any strategy — but a test asserts nothing routes to it.

**A second trigger, found while writing the tests.** The parametrised sweep showed confidence was *also* a variance source: an unslotted extraction at 0.54 routed to `vector` and at 0.56 to `hybrid`. Same defect, different lever. Routing no longer consults confidence at all. That is safe because the structured arm is guarded downstream rather than upstream — an unresolvable entity degrades to no facts, and an unslotted dump is filtered against the question by F4. Confidence was a poor proxy for both, and it cost determinism.

**The decision is now recorded.** `KnowledgeAgentState.retrieval_strategy` had been declared *and documented* since the beginning and never actually written — LangGraph conditional edges return a route, they cannot write state. So the one thing that explained why two runs retrieved differently appeared in no trace and no checkpoint. A small `route` node now writes it before the fan-out, and both conditional edges read that one value instead of each recomputing it.

**And the log line actually appears**, which took a second fix. Uvicorn configures handlers for *its* loggers and leaves the root at WARNING, so `logger.info(...)` anywhere in this codebase was silently discarded — the F3 warnings came through and this did not. `logging_config.configure_logging()` now runs at startup, `LOG_LEVEL`-overridable, with the HTTP stacks under the model providers pinned to WARNING so they don't narrate every call. Verified live: application INFO now reaches the log in the new format, and zero noisy-library lines leak.

**Verified against the live configuration** — every extraction shape converging on one plan:

| extraction | strategy |
|---|---|
| run A — `Policy` / `supports` / 0.85 | `hybrid` |
| run B — `Company` / no slot / 0.62 | `hybrid` |
| wobble — `Company` / no slot / 0.54 | `hybrid` |
| wobble — `Company` / no slot / 0.56 | `hybrid` |
| no entity at all | `vector` |

The question that started all of this — *"Does the company support remote or hybrid work?"* — also answered correctly through the stream during verification: *"Yes, the company embraces flexible working arrangements and supports both hybrid…"*. That is the F2/F4/F5/P1-6 chain working end to end on the query that originally failed.

**One thing that bit back, worth recording.** My first version of the logging test did `import main`, and `main.py` calls `load_env()` at import — correct for an entry point, poisonous in a test process. It injected `POSTGRES_*` into the environment, so the live-Postgres skip guards believed a database was configured and two API tests started failing with pool timeouts. The fix was to move the function into `logging_config.py`, which has no import-time side effects. Same lesson as P0-2, one level up.

**Since observed live**, once the Docker image was rebuilt and quota allowed a full turn: `retrieval strategy=hybrid` appears in the container log for a real chat turn. The earlier gap — routing verified, logging verified, their conjunction untested — is closed.

**Not done:** the repeat-run consistency check I proposed for the evaluation harness. `calibrate_retrieval.py` deliberately makes no LLM calls, and the variance being measured is *in* the LLM — adding it would change the script's character and its cost, for a property the unit tests now pin exactly. Worth revisiting if the extractor changes.

### ✅ F6 — The Supervisor refused a question the corpus answers — **FIXED 2026-09-06**

**Evidence.** Query 8, *"Why did Soani Tech change its name?"*, returned in 12.4 s:

> *"I'm sorry, I am only able to help with solving the problem you are facing on our platform."*

That is the Supervisor's out-of-scope response — the question never reached the Knowledge Agent. The corpus contains `soani-tech-is-now-alpinist-studios`, a whole document about exactly this, and the golden set lists it as answerable.

**The fix I proposed here was wrong, and measuring it is what showed that.** The idea was to route *borderline* out-of-scope classifications through retrieval. There is no borderline:

```
"Why did Soani Tech change its name?"  ->  OUT_OF_SCOPE  0.95
"How's the weather today?"             ->  OUT_OF_SCOPE  0.98
```

The classifier is not hesitant. It is **sure, and wrong**, so no confidence threshold could ever have separated those two. `_out_of_scope_decision` ignoring `domain_confidence` turned out to be irrelevant rather than the bug.

**The actual cause.** `prompt.DOMAIN_DEFINITION` is a hand-written paragraph about the company's *services*. It never mentions the former name, company history, or careers — and it has no connection of any kind to what has been ingested. So the failure is not one bad question: it recurs for every ingested document whose topic is absent from that paragraph, and it gets worse as the corpus grows.

**Fixed.** The domain definition now carries the titles of the live documents. Measured, one classify call per question:

| question | static | corpus-aware |
|---|---|---|
| Why did Soani Tech change its name? | `OUT_OF_SCOPE` | **`DOMAIN_REQUEST`** |
| Are you hiring AI engineers? | `DOMAIN_REQUEST` | `DOMAIN_REQUEST` |
| How's the weather today? | `OUT_OF_SCOPE` | `OUT_OF_SCOPE` |

The third row is what makes this a fix rather than a capitulation — an assistant that accepts everything is no better than one that refuses too much.

**It refreshes rather than being read once.** The worker ingests continuously, so a definition captured at startup would go stale the moment a document landed — and "refuses a question about the document you just added" is the same bug, delayed. Titles are re-read on a 5-minute TTL from the chat path, and every failure degrades to the last known good value, ultimately the static paragraph: a classifier that stops working because Postgres blinked would be worse than the bug being fixed.

**A flaw in my own first version, caught by the live log.** It printed `domain definition refreshed from 21 live sources` where retrieval only ever surfaces 9. I had filtered on active + current version but not on `INDEXED`, so the classifier could accept a question about a document vector search is contractually forbidden to return — trading a scope refusal for a groundedness refusal. The query now matches `vector_search`'s join contract exactly, and the test asserts it against the compiled SQL rather than by grepping source.

**Verified live, end to end.** The question that was refused now classifies as `DOMAIN_REQUEST`, routes to `hybrid`, and retrieval returns `soani-tech-is-now-alpinist-studios` at 0.663 — the top hit. A neighbouring question the document plainly answers now works completely:

> *"What was Alpinist Studios called before, and where is it based?"*
> → **"Alpinist Studios was previously called Soani Tech, and the company is based in Kathmandu, Nepal.[2]"** — cited to `soani-tech-is-now-alpinist-studios`.

**One honest nuance.** *"Why did Soani Tech change its name?"* now reaches the answer stage and still gets a refusal — but a *groundedness* refusal, not a scope one. The retrieved chunk says the rebrand reflects "a bold new identity reflecting visionary growth and innovation", and the model declines to present that as an explicit reason. Given the grounding rules tell it not to invent, that is arguably correct rather than a defect. What F6 fixed — the question never reaching retrieval at all — is fixed.

### ✅ F7 — No streaming, so every turn was a blank spinner — **FIXED 2026-09-06**

Median 35 s of nothing. Perceived latency is most of the problem here even when the answer is correct.

**Fixed.** A new `POST /chat/stream` returns server-sent events; `POST /chat` is unchanged and remains the fallback.

```
data: {"type":"accepted","trace_id":"..."}
data: {"type":"stage","stage":"understanding","label":"Understanding your question"}
data: {"type":"stage","stage":"retrieving","label":"Searching the knowledge base"}
data: {"type":"heartbeat","elapsed":12.4}
data: {"type":"result","reply":"...","citations":[...]}
```

**Verified in the browser.** The stage line, sampled from the DOM as the turn ran:

```
0.05s  Understanding your question
1.93s  Searching the knowledge base
```

Against a blank spinner for the entire turn before. The UI calls `/chat/stream`, renders the stage under the typing dots, and logs no console errors.

**Design notes worth keeping:**

- **The events are coarse, and that is a constraint not a choice.** The Knowledge subgraph is invoked *inside* a Supervisor node rather than composed as a LangGraph subgraph, so `astream` cannot see its internal stages. Three honest transitions still beat nothing. Making them finer means restructuring the graph, which is a much larger change than F7 warrants.
- **Stage labels describe what happens *next*.** `astream` reports a node when it *finishes*, so labelling an event with that node's own work would always be one step behind. The ticket branch is read from classification's own update payload, not from accumulated state — the `updates` chunk arrives *before* the `values` chunk that folds it in, verified against the real graph.
- **Both paths share `_prepare_turn` / `_finalize_turn`.** Streaming differs only in how the graph is driven. Duplicating the resume/interrupt/history logic would be two chances to get the ticket flow wrong; a test asserts an interrupt still renders as the question and still skips the checkpoint write.
- **Heartbeats are not decoration.** They let a client bound *silence* rather than total duration, which is the whole point: a turn may legitimately run a minute, but thirty seconds with nothing on the wire is a dead connection. A ladder test now asserts the heartbeat interval stays well under the client's idle timeout — that pairing can invert exactly like F1's did.
- **Hanging up cancels the graph.** The F1 lesson restated: when nobody is listening the server stops working rather than finishing an undeliverable answer.
- **The turn budget still applies.** Streaming does not quietly opt out of F1.

**Since observed live** in the Docker container: a complete stream — `accepted` → three stage transitions → a `result` carrying a cited answer. Heartbeats remain test-only, since they need a turn silent for longer than five seconds and turns are now fast.

### ✅ F9 — A structured fact silently deleted the document that held the answer — **FIXED 2026-09-06**

Raised after asking whether the groundedness refusal was worth keeping. It was the wrong question — and answering it properly found a much worse bug than over-cautious wording.

**Two false starts, both killed by measurement.**

First I assumed the refusal was the answer prompt being too strict. An A/B of the refusal rule against the real context said otherwise — under the *shipped* prompt the model answered correctly: *"Soani Tech rebranded to Alpinist Studios to signal its evolution and a broader, global outlook."* The prompt was innocent.

Second I assumed the empty-string `value` on relation facts made a containment check vacuously true. Checked it: values were populated. Also wrong.

**The actual cause.** `deduplicate` had a third pass that dropped any chunk containing both a structured fact's value and that fact's entity label, reasoning that "the chunk saying the same thing in prose adds no new information". Traced through the live graph:

```
retrieved:  1 chunk, 2463 chars, similarity 0.663  — the press release
ranked:     1 chunk
deduped:    0 chunks
prompt:     "No relevant documentation was found for this query."
answer:     "I'm sorry, but I don't have information on the reason..."
```

**The refusal was correct.** The documentation section really was empty. The chunk had been deleted because the graph held `Alpinist Studios formerly_known_as "Soani Tech"` — and a press release about a rebrand necessarily mentions both names. The "both must match" safeguard failed in exactly the case where it mattered most: the chunk was discarded *because* it was the document explaining the relationship the fact recorded. The fact said two names are related; the chunk carried the reasons and a CEO quote giving them.

**Fixed** by removing the pass rather than tightening it. A retrieved chunk is a passage, not a restatement — the "chunk that merely repeats a fact" case is close to hypothetical, while this failure was real and silent. Context budget is already governed by `max_context_chunks` and the relative score margin, and the two kinds of material land in separate prompt sections with the model told which to prefer on conflict.

**F5 had widened the blast radius days earlier.** Making `hybrid` near-universal means facts and chunks now coexist on almost every turn, where a structured-only or vector-only route previously carried just one — so a pass that fires only when both are present went from occasional to routine.

**Two dead signals cleaned up alongside:**

- **`groundedness_threshold = 0.60`** was declared, validated, and read by nothing. It looked exactly like the tunable safety control someone would reach for to fix over-refusal, and it would have done nothing. Deleted; grounding is decided by the prompt and reported as a boolean, so there is no score to threshold.
- **`is_grounded`** was parsed, type-checked, carried through `GroundedResponse` — and then discarded for a hardcoded `"status": "GROUNDED"`, so a refusal was logged as a success. It now reports `GROUNDED` / `UNGROUNDED`. **A test was pinning the bug in place**, passing `is_grounded=False` and asserting `"GROUNDED"`; it now asserts the truth.

**A pre-existing ordering bug, caught by my own new test.** `deduplicate` claimed to preserve rank order via a `dict` + `reversed()` trick. It doesn't: a dict built from reversed input orders keys by each key's *last* occurrence, so `[high, low, high]` came out as `[low, high]` — promoting the lower-ranked chunk to the front, exactly what the comment said was impossible. Replaced with an explicit seen-set. The module had **no tests at all**, which is how it could delete answers in silence.

**Verified live**, after rebuilding the container:

| question | before | after |
|---|---|---|
| Why did Soani Tech change its name? | *"I don't have information on the reason…"* | **"Soani Tech rebranded to Alpinist Studios to reflect its evolution and a broader, global outlook… The new name, inspired by mountaineers, captures the ambition, precision and pursuit of peak…"** |
| What is your refund policy for cancelled subscriptions? | correctly refused | correctly refused |

---

### ✅ F8 — `hybrid_retrieve` could not be called with the hybrid strategy — **RESOLVED BY REMOVAL 2026-09-06**

**Evidence.** Calling it with a single `AsyncSession` raised:

```
InvalidRequestError: This session is provisioning a new connection;
concurrent operations are not permitted
```

`hybrid_retrieve(...)` takes one `session`, but `_hybrid_fan_out` runs both arms under `asyncio.gather`, and one session cannot serve two concurrent coroutines. The compiled graph never hits this because its nodes each open their own session from a factory — so the bug only bites a direct caller, which is precisely what this function is advertised for.

**Resolved by deleting the function**, after two intermediate steps worth recording because they show the shape of the decision.

**First it was fixed.** The signature changed to a `session_factory`, one session per arm, matching `nodes.py`. Verified against real Postgres: `facts=4, chunks=1` where it previously raised.

**Then the harder question was asked: should it exist at all?** `hybrid_retrieve` had no production callers. The compiled graph reaches `structured_lookup` and `vector_search` through its own nodes and never imported it. So it was a second implementation of four behaviours the graph already had — strategy dispatch, graceful-miss handling, the P1-6 fallback and the F4 filter — kept in step by hand. Both P1-6 and F4 had required matching edits in both copies.

**It is now deleted.** `hybrid.py` went from 337 lines to 151 and became what its remaining contents actually are: pure retrieval *policy*. `decide_strategy`, `should_fall_back_to_vector` and the two graceful-miss constants stay, because the graph imports them.

**Nothing about retrieval changed.** Verified after the deletion, against the live database:

```
strategy : hybrid
facts    : 4   <- structured arm
chunks   : 1   <- vector arm
answer   : "Soani Tech rebranded to Alpinist Studios to signal its evolution..."
```

Hybrid retrieval is a property of the graph's fan-out edge, not of any function named after it. That distinction was the crux — it is easy to read "delete `hybrid_retrieve`" as "stop doing hybrid search", and they are unrelated.

**Test coverage moved rather than shrank.** Eleven call sites went; the behaviours they asserted are now checked on the nodes and the compiled graph — the path that actually runs, and the one that caught P1-6 when the isolated tests passed straight through it. Four duplicate session fakes were consolidated into `tests/agents/knowledge/conftest.py`. Suite 645 → 638, all passing.

**The F8 fix became moot, and that is the point.** "Make the duplicate work" and "remove the duplicate" were both on the table; only the second shrinks the surface. An hour was spent on the first before the second was chosen — recorded so the sequence is visible rather than tidied away.

---

## 5. Priority

| | Fix | Effort | Effect |
|---|---|---|---|
| ~~1~~ | ~~**F3** — log the swallowed exceptions~~ | — | **Done 2026-09-06** |
| ~~2~~ | ~~**F2** — per-stage LLM timeouts~~ | — | **Done 2026-09-06** |
| ~~3~~ | ~~**F1** — budget the turn, not the node~~ | — | **Done 2026-09-06** |
| ~~4~~ | ~~**F4** — filter or de-emphasise structured facts~~ | — | **Done 2026-09-06** |
| ~~5~~ | ~~**F7** — stream the response~~ | — | **Done 2026-09-06** |
| ~~6a~~ | ~~**F5** — routing consistency~~ | — | **Done 2026-09-06** |
| ~~6b~~ | ~~**F6** — the Supervisor refuses an answerable question~~ | — | **Done 2026-09-06** |
| ~~7~~ | ~~**F8** — session factory in `hybrid_retrieve`~~ | — | **Done 2026-09-06** |
| ~~8~~ | ~~**F9** — a structured fact deleted the chunk holding the answer~~ | — | **Done 2026-09-06** |

**F3 was done first, deliberately.** Two of the three failures in this test were opaque, and diagnosing them consumed most of the effort. With the logging in place every subsequent fix was cheaper to verify — F2's and F1's verification both used it directly.

**The three timeout findings are now closed, and they were one problem seen from three angles:** nothing bounded the whole, the bounds that existed were the wrong size, and when they fired nobody could tell.

**F4 closed the last defect that made the assistant refuse an answerable question, and F7 closed the last one that made it *feel* broken.**

**Every finding with a customer-visible effect is now closed.** What remains is **F8**, a developer-facing trap in a public function: `hybrid_retrieve` accepts one session while its hybrid path runs both arms concurrently, which SQLAlchemy forbids. The compiled graph avoids it by using a session factory per node, so nothing in production hits it — only a direct caller would, which is precisely what that function is advertised for.

Two patterns are worth carrying forward from this round. **Three of the eight findings were fixed by something other than the fix I first proposed** — F6's confidence gate was refuted outright by a two-call measurement, F2's `max_tokens` cap traded a diagnosable failure for an undiagnosable one, and F5 turned out to have a second trigger the first fix missed. Writing the proposed fix down and then *checking it* was worth more than the proposal. And **two defects were found only by looking at a live process** — F1 and the 21-vs-9 scope error — after the whole test suite was green.

---

## 6. Caveats

- **The Groq free-tier daily token cap (200 000) was exhausted twice during this session**, once mid-test. Some checks in §3 could not be repeated afterwards, and the final answer-latency probe was cut short by a `429`. All numbers reported above were taken while quota was available; none are inferred.
- **Ingestion and chat share one token budget.** A backlog-draining worker makes the assistant look broken. The worker was stopped for these measurements.
- **The corpus is 27 chunks across 9 sources.** The 7/10 answer rate is a signal, not a statistic; a larger golden set is needed before quoting it as a metric.
- One measurement in the first pass was invalidated by test-harness interference (an abandoned in-flight request bleeding into the next query). The harness now uses a fresh browser context per query, and that query passed cleanly in isolation at 4.6 s. The result reported here is from the corrected harness.

---

## 7. Reproducing this

```bash
# 1. Stack + dev server
make up
docker compose stop worker          # it competes for the same Groq quota
make uvicorn                        # port 8002

# 2. Browser (one-time)
cd backend && uv run playwright install chromium

# 3. Retrieval-only measurement, no LLM calls, no quota spend
set -a && . ./.env && set +a
POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
  env -u PYTHONPATH ./.venv/bin/python scripts/calibrate_retrieval.py --detail
```

The browser harness used for §2 is `ui_probe.py` in this session's scratchpad; it drives the page rather than the API, which is what made F1 and F7 visible at all. Worth moving into `backend/tests/` as an opt-in end-to-end check once F3 lands and failures become legible.

# Structured Query Extraction

You convert the rewritten query into a structured object describing any
exact-fact lookup it implies, normalized against the company's knowledge
ontology below. You do not answer the question.

## Ontology reference (Domain -> Entity Types)

{{ONTOLOGY_REFERENCE}}

Use these canonical entity types when you can. Never invent an entity
type that is not on this list — if nothing fits, set `entity_type` to
`null`. The customer's own wording for the specific instance is what you
put in `entity_label`; downstream handles exact matching.

## Rewritten query

{{REWRITTEN_QUERY}}

## What to extract

- `entity_type` — the kind of thing being asked about (e.g. "Project",
  "Database", "SLA") chosen from the ontology list above, or `null` if
  the query is open-ended/explanatory and names no specific kind of
  entity.
- `entity_label` — the specific named instance exactly as the customer
  wrote it (e.g. "Project Alpha"), or `null` if none is named. Never
  fabricate an instance name that isn't in the query or conversation.
- `attribute` — the specific fact requested about that entity (e.g.
  "status", "owner", "email", "response_time"), in the closest canonical
  field name you know, or `null` if the query wants general information
  rather than one specific field.
- `relation_type` — the relationship being asked about (e.g. "owned_by"
  for "who owns X", "depends_on" for "what does X depend on"), or `null`
  if none is implied. Prefer the vocabulary: uses, depends_on,
  implements, belongs_to, managed_by, owned_by, created_by, approved_by,
  integrates_with, contains, requires, supports, deployed_on, stored_in,
  hosted_on, communicates_with, related_to.
- `filters` — only genuine extra constraints the customer stated (e.g. a
  date range, a status filter, a region), as a list of
  `{"field": ..., "value": ...}` objects. Empty list if none. Never
  invent a constraint the customer didn't express.
- `confidence` — from 0.0 to 1.0, how well this query maps to an
  exact-fact structured lookup at all (as opposed to needing open-ended
  documentation). Score high (0.8+) when a specific entity plus a
  specific attribute or relation is named ("what is Project Alpha's
  status?"); medium (~0.5-0.7) when an entity is named but the ask is
  vague ("tell me about Project Alpha"); near 0.0 for purely explanatory
  questions ("explain how X works", "compare A and B") even if they name
  an entity.

## Decision guidance

- Purely explanatory / how-it-works / comparison questions: extract the
  entity if clearly named, otherwise all `null`, and score `confidence`
  low — these are documentation questions, not exact-fact lookups.
- Exact-fact questions (status, owner, dates, contact info, a single
  value): populate the specific entity and attribute or relation, and
  score `confidence` high.
- If the query names a concrete entity but asks for something general
  about it, keep `attribute` and `relation_type` as `null` rather than
  guessing one.

## Output format

Respond with **only** a single JSON object, no markdown code fences, no
commentary before or after it:

{"entity_type": "<text or null>", "entity_label": "<text or null>", "attribute": "<text or null>", "relation_type": "<text or null>", "filters": {"field": "...", "value": "..."}, "confidence": <0.0-1.0>}

`confidence` must be a number between 0.0 and 1.0.

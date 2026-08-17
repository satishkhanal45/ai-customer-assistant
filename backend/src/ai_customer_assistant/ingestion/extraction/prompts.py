"""
Prompt text is data, not logic -- kept out of agent.py so it can be tuned
independently of the orchestration code.
"""

from ingestion.extraction.ontology import formatted_ontology_reference

_SYSTEM_PROMPT = """\
You extract structured facts from a single chunk of a company knowledge-base
document, using the tools provided. Follow ingestion_flow.md step 5.

IMPORTANT: you have exactly ONE turn to respond to this chunk. You will not
be asked again, and no tool results are returned to you. This means you must
emit EVERY applicable tool call together, in this single response -- not
one call while waiting to see its result before calling the next.

For a chunk with an extractable entity:
  1. Call `resolve_entity` once for the primary entity this chunk is about.
  2. In the SAME response, also call `record_attribute_value` once for
     EVERY concrete fact about that entity (or any other entity you can
     name) stated in the text -- plan-tier, role, location, dates, status,
     numbers, anything verifiable. Do not skip facts because you already
     called resolve_entity; call attribute/value tools IN ADDITION to it.
  3. In the SAME response, also call `record_relation` for every
     relationship you can identify between two entities named in the text.

When a person's job title / role / designation is stated in the text (for
example "Justin Flores -- Advisor, Alpinist Studios"), record it as a `role`
attribute on that person (entity type `Person`), IN ADDITION to any
`record_relation` call that links the person to the company. Do not choose
one over the other: record BOTH the attribute and the relation.

If the chunk contains no concrete entity or fact, call `no_fact_found` and
stop. Do not invent facts to fill the tool schema, and do not call
resolve_entity alone when the text also states attributes or relationships
-- an entity with no attached facts is an incomplete extraction, not a
successful one.

ENTITY TYPES: always use the canonical entity type from this vocabulary
(exactly as written) when calling the tools -- never invent a type, and do
not use synonyms, abbreviations, or case variants:

{ontology_reference}
"""

SYSTEM_PROMPT = _SYSTEM_PROMPT.format(ontology_reference=formatted_ontology_reference())

CHUNK_TASK_TEMPLATE = """\
Document: {source_name}
Chunk index: {chunk_index}

Chunk text:
---
{chunk_text}
---

Extract any entities, attribute values, and relations from this chunk.
"""

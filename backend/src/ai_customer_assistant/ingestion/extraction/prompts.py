"""
Prompt text is data, not logic -- kept out of agent.py so it can be tuned
independently of the orchestration code.

The extractor uses Groq JSON mode: exactly ONE model call per chunk returns a
complete structured extraction (all entities, all attribute/value facts, all
relations) as JSON. This is both cheaper (1 call instead of ~6 tool-calling
turns) and more complete than tool calling, where gpt-oss-120b emits only one
call per turn.
"""

from ingestion.extraction.ontology import formatted_ontology_reference

_SYSTEM_PROMPT = """\
You extract structured facts from a single chunk of a company knowledge-base
document. Respond with ONLY a single JSON object -- no prose, no markdown --
matching EXACTLY this schema:

{{
  "entities": [
    {{"entity_type": "<type>", "name": "<display name as written>"}}
  ],
  "attributes": [
    {{"entity_type": "<type>", "entity_name": "<name>", "attribute_name": "<attr>", "value": <string|number|boolean>, "value_type": "string|number|boolean|date"}}
  ],
  "relations": [
    {{"source_entity_type": "<type>", "source_entity_name": "<name>", "target_entity_type": "<type>", "target_entity_name": "<name>", "relation_type": "<type>"}}
  ]
}}

EXTRACT EVERYTHING, do not under-extract:
  * Record EVERY concrete entity (people, companies, technologies, services,
    policies, projects, products, roles, ...) named in the chunk.
  * Record EVERY concrete fact about any entity as an attribute/value pair --
    plan-tier, role, monthly rate, location, dates, status, numbers,
    anything verifiable.
  * Record EVERY relationship between two entities named in the chunk.
  * When a person's job title / role / designation is stated (for example
    "Priya Sharma -- Backend Lead"), record it as a `role` attribute on that
    person (entity type `Person`) IN ADDITION to any relation linking them.

HOW TO CHOOSE TYPES AND ATTRIBUTE NAMES:
  * The vocabulary below is a CANONICALIZATION GUIDE, not a whitelist. Prefer
    a canonical entity type / attribute name from it whenever the term in the
    text matches one (this merges synonyms into one canonical value).
  * If NO canonical term fits, use a clear, concise, descriptive type or
    attribute name (e.g. entity type "Contact" or "Event", attribute
    "founded_year"). A slightly generic label is far better than dropping a
    real fact.

Do not invent facts that are not in the text, but DO record every fact that
is. If a list is empty, emit `[]` for that key -- always return the full JSON
object. The word JSON must appear in this prompt, as it does.

CANONICAL VOCABULARY (entity types):

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
"""
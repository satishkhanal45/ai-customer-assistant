"""Shared ontology: the one vocabulary both pipelines canonicalize against.

`vocabulary` holds the tables (entity types, synonyms, per-type attributes,
value types, relation types). `matching` holds the pure resolution engine over
them. Neither imports from `agents` or `ingestion`, so both can depend on this
package without depending on each other.

See `vocabulary.py`'s docstring for why this exists — the two packages
previously carried forked copies that drifted apart silently.
"""

from .matching import (
    Match,
    attributes_for_entity_type,
    match_attribute,
    match_entity_type,
    match_relation_type,
    normalize,
    value_type_for_attribute,
)

__all__ = [
    "Match",
    "attributes_for_entity_type",
    "match_attribute",
    "match_entity_type",
    "match_relation_type",
    "normalize",
    "value_type_for_attribute",
]

"""
constants.py — static constants for the Knowledge Agent.

Pure data layer: literal values and small immutable lookup tables that
other modules reference by name instead of hardcoding magic numbers or
strings inline. Nothing here has behavior. Defaults that are meant to be
overridable at runtime live in `config.py` (`KnowledgeAgentConfig`) and
*reference* these constants as their default values — this file is the
single place those defaults are defined.
"""

from __future__ import annotations

from types import MappingProxyType

# --------------------------------------------------------------------------
# Retrieval strategy literals
# --------------------------------------------------------------------------

STRATEGY_STRUCTURED: str = "structured"
STRATEGY_VECTOR: str = "vector"
STRATEGY_HYBRID: str = "hybrid"

RETRIEVAL_STRATEGIES: tuple[str, ...] = (
    STRATEGY_STRUCTURED,
    STRATEGY_VECTOR,
    STRATEGY_HYBRID,
)

# --------------------------------------------------------------------------
# schema.md `attribute.value_type` CHECK constraint — the only five
# values ontology.py's value_type_for_attribute() is allowed to return.
# --------------------------------------------------------------------------

VALUE_TYPE_STRING: str = "string"
VALUE_TYPE_NUMBER: str = "number"
VALUE_TYPE_BOOLEAN: str = "boolean"
VALUE_TYPE_DATE: str = "date"
VALUE_TYPE_JSON: str = "json"

VALID_VALUE_TYPES: tuple[str, ...] = (
    VALUE_TYPE_STRING,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_BOOLEAN,
    VALUE_TYPE_DATE,
    VALUE_TYPE_JSON,
)

# --------------------------------------------------------------------------
# knowledge_source_version.status — only INDEXED rows are ever eligible
# for vector search, per the mandated join contract.
# --------------------------------------------------------------------------

VERSION_STATUS_INDEXED: str = "INDEXED"

# --------------------------------------------------------------------------
# knowledge_source_entity_map.relationship_type — the value that marks a
# chunk as linked to a resolved business entity.
# --------------------------------------------------------------------------

RELATIONSHIP_TYPE_DERIVED_CHUNK: str = "DERIVED_CHUNK"

# --------------------------------------------------------------------------
# Retrieval defaults (referenced by config.py as field defaults)
# --------------------------------------------------------------------------

DEFAULT_TOP_K: int = 8

# Absolute cosine-similarity floor below which a chunk is not a hit at all.
#
# This was 0.70, chosen without evidence, and it was badly wrong. Measured by
# `scripts/calibrate_retrieval.py` against the live corpus and the golden set
# in `tests/data/golden_retrieval.json` (26 answerable questions, 10 the
# corpus genuinely cannot answer), a 0.70 floor kept the correct source for
# only **5 of 26** answerable questions — and for most of the 21 it rejected,
# the correct document was sitting at rank 1. The system had the right answer
# and refused to use it.
#
# The measured populations overlap: answerable questions score 0.480-0.793 at
# the top, unanswerable ones 0.357-0.557. No floor separates them cleanly, so
# the choice is which error to prefer. 0.50 keeps 25/26 answerable questions
# and lets 2/10 unanswerable ones through.
#
# Preferring recall is deliberate, because the two failures are not
# symmetrical. An irrelevant chunk that gets through still has to survive the
# answer prompt and `groundedness_threshold` before it reaches a customer. A
# rejected chunk has no second chance: the turn simply fails, and the
# customer is told nothing is known about a question the corpus answers.
#
# So this is now a sanity floor — it rejects "what is the recipe for
# sourdough bread" (0.388), not near-misses — and the real discrimination
# happens in DEFAULT_RELATIVE_SCORE_MARGIN below and in the answer prompt's
# own grounding rules. Re-run the calibration script after any corpus or model change;
# these numbers are specific to both.
DEFAULT_SIMILARITY_THRESHOLD: float = 0.50

# How far below the best hit a chunk may score and still be kept.
#
# An absolute floor cannot separate "these five chunks are all about the
# question" from "nothing here is relevant but everything scores 0.68",
# because normalised BGE cosine similarity is compressed: unrelated English
# text routinely lands in the 0.6-0.7 band, so a single global cutoff is
# simultaneously too permissive (noise passes) and too strict (a genuinely
# good answer to an oddly-worded question is dropped).
#
# The margin is relative to the query's own best result, which is the signal
# an absolute floor throws away: if the top hit scores 0.83, a chunk at 0.66
# is not in the same conversation, whereas if the top hit scores 0.71 a chunk
# at 0.66 probably is. Set to 0.0 to disable and keep only the absolute floor.
#
# 0.12 is measured, not guessed. Swept over the golden set, it keeps the
# correct source for **26 of 26** answerable questions while cutting the mean
# result set from 8 chunks to 4.5 — half the context, no recall lost. Tighter
# margins start costing recall (0.05 and 0.08 both drop to 25/26); looser ones
# buy nothing back. A margin can only ever discard a correct source that
# failed to rank first, so this sweep is the whole risk.
DEFAULT_RELATIVE_SCORE_MARGIN: float = 0.12
DEFAULT_EXTRACTION_CONFIDENCE_THRESHOLD: float = 0.55
DEFAULT_MAX_CONTEXT_CHUNKS: int = 12
DEFAULT_MAX_STRUCTURED_FACTS: int = 20

# --------------------------------------------------------------------------
# Embedding model — must match chunk_embed's IngestionSettings so
# query-time and index-time vectors live in the same space.
# --------------------------------------------------------------------------

DEFAULT_EMBEDDING_MODEL_NAME: str = "BAAI/bge-base-en-v1.5"
DEFAULT_EMBEDDING_DIMENSION: int = 768

# BAAI's `bge-*-en-v1.5` models are trained for *asymmetric* retrieval: the
# passage side is encoded bare, and the query side is encoded with this exact
# instruction prepended. The model card documents it verbatim, trailing space
# included.
#
# This project encoded both sides bare, so every query was embedded under a
# different convention than the model was trained for. Index-time was already
# correct (`ingestion/chunk_embed/embedding.py` embeds `chunk.text` as-is),
# which is why adding the query prefix needs no re-indexing: only the query
# side changes, and it changes toward what the weights expect.
BGE_QUERY_INSTRUCTION: str = "Represent this sentence for searching relevant passages: "

# --------------------------------------------------------------------------
# Prompt template file names (relative to KnowledgeAgentConfig.prompts_dir)
# --------------------------------------------------------------------------

PROMPT_TEMPLATE_REWRITE: str = "rewrite.md"
PROMPT_TEMPLATE_EXTRACTION: str = "extraction.md"
PROMPT_TEMPLATE_ANSWER: str = "answer.md"

PROMPT_TEMPLATE_NAMES: tuple[str, ...] = (
    PROMPT_TEMPLATE_REWRITE,
    PROMPT_TEMPLATE_EXTRACTION,
    PROMPT_TEMPLATE_ANSWER,
)

# --------------------------------------------------------------------------
# Context builder section headers — kept here (not hardcoded inline in
# context_builder.py) so wording changes don't touch logic.
# --------------------------------------------------------------------------

CONTEXT_SECTION_STRUCTURED_FACTS: str = "Structured Facts"
CONTEXT_SECTION_RELEVANT_DOCUMENTATION: str = "Relevant Documentation"

# --------------------------------------------------------------------------
# Ranking weights — how much each signal contributes to a RankedResult's
# ordering. Kept as a mapping so ranking.py can fold over it declaratively
# rather than branching per signal.
# --------------------------------------------------------------------------

RANKING_WEIGHTS: MappingProxyType[str, float] = MappingProxyType(
    {
        "exact_structured_match": 1.00,
        "semantic_similarity": 0.70,
        "extraction_confidence": 0.20,
        "source_freshness": 0.10,
    }
)
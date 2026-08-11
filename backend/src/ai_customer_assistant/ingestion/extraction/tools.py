"""
The EAV agent uses LangChain *tool calling* as a structured-output
mechanism: the LLM is bound to the pydantic schemas below and asked to
emit zero or more tool calls per chunk. We deliberately do not give the
tools executable bodies that touch the database -- that would smuggle I/O
into what should be a pure "parse the model's structured output" step.

Persisting the resolved entities/attributes/relations is the ingestion
pipeline's job (pipeline.py), using the repository layer, exactly the way
chunk persistence is. This module only turns
`AIMessage.tool_calls` into an immutable `ChunkExtraction`.
"""

from __future__ import annotations

from functools import reduce
from typing import Any

from ingestion.extraction.schema import (
    NoFactFound,
    RecordAttributeValueArgs,
    RecordRelationArgs,
    ResolveEntityArgs,
)
from ingestion.pipeline_types import (
    ChunkExtraction,
    ExtractedFact,
    ExtractedRelation,
)
from langchain_core.utils.function_calling import convert_to_openai_tool


# Every schema the agent may call, keyed by the tool name LangChain derives
# from the pydantic model (its class name).
TOOL_SCHEMAS: tuple[type, ...] = (
    ResolveEntityArgs,
    RecordAttributeValueArgs,
    RecordRelationArgs,
    NoFactFound,
)

_TOOL_NAME_OVERRIDES: dict[type, str] = {
    ResolveEntityArgs: "resolve_entity",
    RecordAttributeValueArgs: "record_attribute_value",
    RecordRelationArgs: "record_relation",
    NoFactFound: "no_fact_found",
}

def _tool_def(schema: type) -> dict:
    tool = convert_to_openai_tool(schema)
    tool["function"]["name"] = _TOOL_NAME_OVERRIDES[schema]
    return tool

TOOL_DEFS: tuple[dict, ...] = tuple(_tool_def(schema) for schema in TOOL_SCHEMAS)

_TOOL_NAMES = {
    name: schema for schema, name in _TOOL_NAME_OVERRIDES.items()
}


def _apply_resolve_entity(acc: ChunkExtraction, args: ResolveEntityArgs) -> ChunkExtraction:
    return ChunkExtraction(
        chunk_index=acc.chunk_index,
        entity=(args.entity_type, args.name),
        facts=acc.facts,
        relations=acc.relations,
    )


def _apply_attribute_value(
    acc: ChunkExtraction, args: RecordAttributeValueArgs
) -> ChunkExtraction:
    fact = ExtractedFact(
        entity_type=args.entity_type,
        entity_name=args.entity_name,
        namespace=args.namespace,
        attribute_name=args.attribute_name,
        value=args.value,
        value_type=args.value_type,
        multivalue=args.multivalue,
        searchable=args.searchable,
    )
    return ChunkExtraction(
        chunk_index=acc.chunk_index,
        entity=acc.entity,
        facts=(*acc.facts, fact),
        relations=acc.relations,
    )


def _apply_relation(acc: ChunkExtraction, args: RecordRelationArgs) -> ChunkExtraction:
    relation = ExtractedRelation(
        source_entity_type=args.source_entity_type,
        source_entity_name=args.source_entity_name,
        target_entity_type=args.target_entity_type,
        target_entity_name=args.target_entity_name,
        relation_type=args.relation_type,
    )
    return ChunkExtraction(
        chunk_index=acc.chunk_index,
        entity=acc.entity,
        facts=acc.facts,
        relations=(*acc.relations, relation),
    )


def _apply_no_fact(acc: ChunkExtraction, _args: NoFactFound) -> ChunkExtraction:
    return acc


# Dispatch table: emitted tool name -> (schema, filler). The model calls the
# tools by their RENAMED snake_case names (from _TOOL_NAME_OVERRIDES), not
# the pydantic class names -- so the keys must be the override names, or every
# tool call is silently dropped and extraction is a no-op.
_HANDLERS: dict[str, tuple[type, Any]] = {
    _TOOL_NAME_OVERRIDES[ResolveEntityArgs]: (ResolveEntityArgs, _apply_resolve_entity),
    _TOOL_NAME_OVERRIDES[RecordAttributeValueArgs]: (RecordAttributeValueArgs, _apply_attribute_value),
    _TOOL_NAME_OVERRIDES[RecordRelationArgs]: (RecordRelationArgs, _apply_relation),
    _TOOL_NAME_OVERRIDES[NoFactFound]: (NoFactFound, _apply_no_fact),
}


def _fold_tool_call(acc: ChunkExtraction, tool_call: dict) -> ChunkExtraction:
    schema, apply = _HANDLERS.get(tool_call["name"], (None, None))
    if schema is None:
        # Unknown tool name (shouldn't happen since bind_tools constrains
        # the model) -- ignore rather than raise, extraction stays total.
        return acc
    return apply(acc, schema.model_validate(tool_call["args"]))


def tool_calls_to_extraction(chunk_index: int, tool_calls: list[dict]) -> ChunkExtraction:
    """
    Pure: fold every tool call the model made for one chunk into a single
    immutable ChunkExtraction. Order is preserved so `resolve_entity`
    naturally lands before the attribute/relation calls that reference it.
    """
    return reduce(
        _fold_tool_call,
        tool_calls,
        ChunkExtraction(chunk_index=chunk_index, entity=None),
    )


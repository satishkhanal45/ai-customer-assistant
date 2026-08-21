"""
EAV extraction agent (ingestion_flow.md step 5).

One JSON-mode model call per chunk. gpt-oss-120b tool calling emits a single
tool call per turn, which made the previous 3-pass tool-calling loop both slow
(up to ~6 calls/chunk) and sparse (a few facts per chunk). JSON mode returns
the complete extraction -- all entities, all attribute/value facts, all
relations -- in a single response, so it is both cheaper and more complete.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from groq import Groq

from ingestion.extraction.ontology import (
    safe_canonicalize_attribute,
    safe_canonicalize_entity_type,
    safe_canonicalize_relation_type,
)
from ingestion.extraction.prompts import (
    CHUNK_TASK_TEMPLATE,
    SYSTEM_PROMPT,
)
from ingestion.extraction.schema import (
    ExtractionOutput,
    ValueType,
    VALUE_TYPES,
)
from ingestion.pipeline_types import (
    ChunkExtraction,
    ExtractedFact,
    ExtractedRelation,
)


@dataclass(frozen=True, slots=True)
class ExtractionAgent:
    """A bound Groq client and model. Immutable -- build once, reuse."""

    client: Groq
    model: str
    max_tokens: int = 1500


def build_extraction_agent(client: Groq, model: str) -> ExtractionAgent:
    return ExtractionAgent(client=client, model=model)


def _build_messages(*, source_name: str, chunk_index: int, chunk_text: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": CHUNK_TASK_TEMPLATE.format(
                source_name=source_name, chunk_index=chunk_index, chunk_text=chunk_text
            ),
        },
    ]


# Retry transient provider errors with backoff: rate limits (429) and flaky
# JSON-output failures. When the API says when the window rolls over ("try
# again in XmYs"), wait at least that long instead of a fixed schedule.
# gpt-oss-120b extraction degrades sharply once the chunk text grows past a
# couple of thousand chars: it drops attributes and relations (or emits
# malformed JSON), so long chunks are split into overlapping windows and each
# window is extracted separately, then merged.
_WINDOW_CHARS = 1800
_WINDOW_OVERLAP = 150

_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 3.0
_MAX_COOLDOWN_WAIT = 420.0  # 7 minutes


def _window_text(text: str, size: int, overlap: int) -> tuple[str, ...]:
    if len(text) <= size:
        return (text,)
    windows = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        windows.append(text[start:end])
        if end == n:
            break
        start = end - overlap
    return tuple(windows)


def _is_retryable(exc: Exception) -> bool:
    text = str(exc)
    lower = text.lower()
    return (
        "429" in text
        or "rate_limit" in lower
        or "rate limit" in lower
        or "json" in lower
    )


def _cooldown_seconds(exc: Exception) -> float | None:
    """Parse Groq's 'Please try again in 6m33.552s.' hint into seconds."""
    text = str(exc)
    for token in text.replace(",", " ").split():
        stripped = token.rstrip(".")
        if "m" in stripped and stripped.endswith("s"):
            minutes_part, seconds_part = stripped[:-1].split("m", 1)
            try:
                return float(minutes_part) * 60 + float(seconds_part)
            except ValueError:
                continue
        if stripped.endswith("m"):
            try:
                return float(stripped[:-1]) * 60
            except ValueError:
                continue
        if stripped.endswith("s"):
            try:
                return float(stripped[:-1])
            except ValueError:
                continue
    return None


def _invoke_with_retry(agent: ExtractionAgent, messages: list[dict]) -> object:
    delay = _RETRY_BASE_DELAY
    for attempt in range(_MAX_RETRIES):
        try:
            return agent.client.chat.completions.create(
                model=agent.model,
                temperature=0,
                max_tokens=agent.max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001
            if attempt == _MAX_RETRIES - 1 or not _is_retryable(exc):
                raise
            cooldown = _cooldown_seconds(exc)
            wait = min(cooldown or delay, _MAX_COOLDOWN_WAIT)
            time.sleep(max(wait, delay))
            delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover


def _coerce_value_type(value_type: str, value: str | int | float | bool) -> str:
    if value_type in VALUE_TYPES:
        return value_type
    lowered = str(value).strip().lower()
    if lowered in ("true", "false"):
        return "boolean"
    try:
        float(lowered)
        return "number"
    except ValueError:
        return "string"


def _output_to_extraction(chunk_index: int, output: ExtractionOutput) -> ChunkExtraction:
    entity = None
    if output.entities:
        first = output.entities[0]
        entity = (safe_canonicalize_entity_type(first.entity_type), first.name)

    facts = tuple(
        ExtractedFact(
            entity_type=safe_canonicalize_entity_type(attr.entity_type),
            entity_name=attr.entity_name,
            namespace="general",
            attribute_name=safe_canonicalize_attribute(attr.entity_type, attr.attribute_name),
            value=str(attr.value),
            value_type=_coerce_value_type(attr.value_type, attr.value),
            multivalue=False,
            searchable=True,
        )
        for attr in output.attributes
    )

    relations = tuple(
        ExtractedRelation(
            source_entity_type=safe_canonicalize_entity_type(rel.source_entity_type),
            source_entity_name=rel.source_entity_name,
            target_entity_type=safe_canonicalize_entity_type(rel.target_entity_type),
            target_entity_name=rel.target_entity_name,
            relation_type=safe_canonicalize_relation_type(rel.relation_type),
        )
        for rel in output.relations
    )

    return ChunkExtraction(chunk_index=chunk_index, entity=entity, facts=facts, relations=relations)


def extract_chunk(
    agent: ExtractionAgent,
    *,
    source_name: str,
    chunk_index: int,
    chunk_text: str,
) -> ChunkExtraction:
    """
    Run extraction for a single chunk: one JSON-mode model call, then a pure
    parse + canonicalize step.
    """
    messages = _build_messages(
        source_name=source_name, chunk_index=chunk_index, chunk_text=chunk_text
    )
    response = _invoke_with_retry(agent, messages)
    content = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = {}
    output = ExtractionOutput.model_validate(payload)
    return _output_to_extraction(chunk_index, output)


def _merge_windows(extractions: tuple[ChunkExtraction, ...]) -> ChunkExtraction:
    """Merge per-window extractions of one chunk. The first window that
    resolves an entity wins (each window sees the document's start). Facts and
    relations simply concatenate; persistence dedups identical values."""
    if not extractions:
        return ChunkExtraction(chunk_index=0, entity=None, facts=(), relations=())
    chunk_index = extractions[0].chunk_index
    entity = next((e.entity for e in extractions if e.entity is not None), None)
    facts = tuple(f for e in extractions for f in e.facts)
    relations = tuple(r for e in extractions for r in e.relations)
    return ChunkExtraction(chunk_index=chunk_index, entity=entity, facts=facts, relations=relations)


def extract_chunk(
    agent: ExtractionAgent,
    *,
    source_name: str,
    chunk_index: int,
    chunk_text: str,
) -> ChunkExtraction:
    """Run extraction for a single chunk. Chunks longer than one model window
    are split and merged so gpt-oss-120b sees bounded input (it otherwise
    under-extracts on long text)."""
    windows = _window_text(chunk_text, _WINDOW_CHARS, _WINDOW_OVERLAP)
    if len(windows) == 1:
        return _extract_window(
            agent, source_name=source_name, chunk_index=chunk_index, chunk_text=windows[0]
        )
    return _merge_windows(
        tuple(
            _extract_window(
                agent, source_name=source_name, chunk_index=chunk_index, chunk_text=w
            )
            for w in windows
        )
    )


def _extract_window(
    agent: ExtractionAgent,
    *,
    source_name: str,
    chunk_index: int,
    chunk_text: str,
) -> ChunkExtraction:
    messages = _build_messages(
        source_name=source_name, chunk_index=chunk_index, chunk_text=chunk_text
    )
    response = _invoke_with_retry(agent, messages)
    content = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = {}
    output = ExtractionOutput.model_validate(payload)
    return _output_to_extraction(chunk_index, output)


def extract_document(
    agent: ExtractionAgent,
    *,
    source_name: str,
    chunks: tuple,  # tuple[chunk_embed.types.EmbeddedChunk, ...]
) -> tuple[ChunkExtraction, ...]:
    """
    Run extraction across every chunk of a document. Reads the real
    EmbeddedChunk shape (`embedded.chunk.chunk_index` / `.text`) directly --
    no adapter object needed. A comprehension, not a for-loop with an
    accumulator list, since each chunk's extraction is independent (step 5
    is scoped per-chunk).
    """
    return tuple(
        extract_chunk(
            agent,
            source_name=source_name,
            chunk_index=embedded.chunk.chunk_index,
            chunk_text=embedded.chunk.text,
        )
        for embedded in chunks
    )
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
import logging
import os
import time
from dataclasses import dataclass

from groq import Groq

from ingestion.extraction.ontology import (
    safe_canonicalize_attribute,
    safe_canonicalize_entity_type,
    safe_canonicalize_relation_type,
)
from ingestion.extraction.prompts import (
    BATCH_SYSTEM_SUFFIX,
    BATCH_TASK_TEMPLATE,
    BATCH_WINDOW_TEMPLATE,
    CHUNK_TASK_TEMPLATE,
    SYSTEM_PROMPT,
)
from ingestion.extraction.schema import (
    BatchedExtractionOutput,
    ExtractionOutput,
    ValueType,
    VALUE_TYPES,
)
from ingestion.pipeline_types import (
    ChunkExtraction,
    ExtractedFact,
    ExtractedRelation,
)

logger = logging.getLogger(__name__)


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

# How long a single 429 cooldown may hold the call.
#
# This was 420 seconds. The stage that contains it is bounded at 600
# (`INGEST_EXTRACTION_STAGE_BUDGET_S`), so one cooldown could consume 70% of
# the budget for the entire document and a second would exceed it outright --
# which is what turned rate limiting into documents that never finished.
#
# 60 seconds is chosen against the thing being waited for: Groq's per-minute
# token bucket refills every minute, so waiting longer than that for a TPM
# limit buys nothing. A longer cooldown hint means the daily quota is gone,
# and no amount of waiting inside one job will fix that -- failing fast and
# leaving the job requeueable is the better answer.
_MAX_COOLDOWN_WAIT = 60.0


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


# Groq rejects a response that does not match the requested JSON schema with
# a 400 whose message contains the word "JSON". That used to satisfy the
# `"json" in lower` clause below, so the call was retried up to five times --
# with `temperature=0` and an identical prompt, producing an identical
# rejection each time. Five times the tokens for a guaranteed failure, spent
# against the daily budget that was the binding constraint.
#
# Repeating a deterministic request is never the answer to it. Sending a
# *smaller* one can be, which is what `_extract_batch_splitting` does.
_DETERMINISTIC_MARKERS = ("json_validate_failed", "context_length", "too large")


def _is_deterministic_rejection(exc: Exception) -> bool:
    """Would this request fail identically however many times it is sent?"""
    lower = str(exc).lower()
    return any(marker in lower for marker in _DETERMINISTIC_MARKERS)


def is_rate_limited(exc: Exception) -> bool:
    """Did the provider throttle us, rather than object to the request?

    Public because the queue's retry policy needs the same answer: a
    throttled document is worth trying again in ten minutes, and a rejected
    one never will be. Keeping the test in one place is what stops the two
    layers drifting into different definitions of the same word.
    """
    if _is_deterministic_rejection(exc):
        return False
    text = str(exc)
    lower = text.lower()
    return "429" in text or "rate_limit" in lower or "rate limit" in lower


def _is_retryable(exc: Exception) -> bool:
    return is_rate_limited(exc)


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


def _invoke_with_retry(
    agent: ExtractionAgent, messages: list[dict], *, max_tokens: int | None = None
) -> object:
    delay = _RETRY_BASE_DELAY
    for attempt in range(_MAX_RETRIES):
        try:
            return agent.client.chat.completions.create(
                model=agent.model,
                temperature=0,
                max_tokens=max_tokens or agent.max_tokens,
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


# NOTE: a second, identical `extract_chunk` used to be defined here and was
# immediately shadowed by the windowing version below -- dead from the moment
# windowing was added. Removed rather than kept: two functions with one name
# means the one you read is not necessarily the one that runs.


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


# How many windows share one model call.
#
# The system prompt is 896 tokens and a window is about 450, so a single
# window call spends two thirds of its input on text the model has already
# been sent. Batching amortises that: at three windows the overhead per
# window drops by ~67%, which on a per-minute token budget is the difference
# between a document finishing and a document timing out.
#
# Three rather than more, because the reason windows exist at all is that
# this model under-extracts on long input. Batching keeps each window
# separately delimited and separately answered, which is not the same as
# handing it one long passage -- but it is not free of that risk either, so
# the batch stays small and the size is tunable.
#
# `INGESTION_EXTRACTION_BATCH_WINDOWS=1` restores exactly the previous
# behaviour, one call per window, and is the escape hatch if a future model
# handles batching worse than this one.
_BATCH_WINDOWS = max(1, int(os.environ.get("INGESTION_EXTRACTION_BATCH_WINDOWS", "3")))

# Output has to grow with the batch or the response is truncated mid-JSON --
# which the model reports as a validation failure and looks like a content
# problem rather than a budget one.
_BATCH_MAX_TOKENS_PER_WINDOW = 1500


def _build_batch_messages(*, source_name: str, windows: tuple[str, ...]) -> list[dict]:
    rendered = "\n".join(
        BATCH_WINDOW_TEMPLATE.format(window_id=i, chunk_text=text)
        for i, text in enumerate(windows)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT + BATCH_SYSTEM_SUFFIX},
        {
            "role": "user",
            "content": BATCH_TASK_TEMPLATE.format(
                source_name=source_name,
                window_count=len(windows),
                windows=rendered,
            ),
        },
    ]


def _extract_batch(
    agent: ExtractionAgent,
    *,
    source_name: str,
    batch: tuple[tuple[int, str], ...],
) -> tuple[ChunkExtraction, ...]:
    """One model call covering several windows. Returns one ChunkExtraction
    per window, in the order given.

    A window the model omits yields an empty extraction rather than an error:
    losing one window's facts is a smaller harm than failing the document,
    and it is logged so the loss is visible rather than silent.
    """
    texts = tuple(text for _, text in batch)
    response = _invoke_with_retry(
        agent,
        _build_batch_messages(source_name=source_name, windows=texts),
        max_tokens=_BATCH_MAX_TOKENS_PER_WINDOW * len(batch),
    )
    content = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = {}

    parsed = BatchedExtractionOutput.model_validate(payload)
    by_id = {window.window_id: window for window in parsed.windows}

    # Positional fallback: some responses come back correctly ordered but
    # without ids. Using position is better than discarding real extractions
    # over a missing integer.
    if not by_id or set(by_id) == {-1}:
        by_id = dict(enumerate(parsed.windows))

    missing = [i for i in range(len(batch)) if i not in by_id]
    if missing:
        logger.warning(
            "Extraction batch for %s returned %d/%d windows; %s produced nothing.",
            source_name,
            len(batch) - len(missing),
            len(batch),
            f"window(s) {missing}",
        )

    return tuple(
        _output_to_extraction(
            chunk_index,
            by_id.get(position) or ExtractionOutput(),
        )
        for position, (chunk_index, _) in enumerate(batch)
    )


class ExtractionFailed(RuntimeError):
    """Extraction produced nothing at all for a document."""


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    extractions: tuple[ChunkExtraction, ...]
    lost_windows: int


def _extract_batch_splitting(
    agent: ExtractionAgent,
    *,
    source_name: str,
    batch: tuple[tuple[int, str], ...],
) -> _BatchOutcome:
    """One call for the batch; on a deterministic rejection, split and retry
    the halves.

    Batching made one bad call expensive: `_extract_batch` raises, and the
    whole document fails, so three windows are lost over one. Two documents in
    this deployment (`sdlc.pdf`, `tech_stck.pdf`) sat at zero chunks for
    exactly that reason, with `json_validate_failed` and an *empty*
    `failed_generation` -- the signature of a response outgrowing what the
    model will emit for three windows at once, not of unextractable content.

    Halving the request is the fix for that, and it is also the fix for the
    other case: if one window genuinely cannot be extracted, the split
    isolates it and only that window is lost.

    Only deterministic rejections split. A rate limit must not: splitting a
    throttled batch makes two throttled calls against a budget that is already
    gone, and `_invoke_with_retry` has already waited out what waiting can fix.
    """
    try:
        return _BatchOutcome(
            _extract_batch(agent, source_name=source_name, batch=batch), 0
        )
    except Exception as exc:  # noqa: BLE001 - re-raised unless splitting helps
        if not _is_deterministic_rejection(exc):
            raise

        if len(batch) == 1:
            # Nothing left to split. Lose the window rather than the document,
            # and say so -- `extract_document` still fails the job if every
            # window ends up here.
            chunk_index, _ = batch[0]
            logger.warning(
                "Extraction failed for a window of chunk %d in %s (%s); "
                "continuing without that window's facts.",
                chunk_index,
                source_name,
                exc,
            )
            return _BatchOutcome(
                (_output_to_extraction(chunk_index, ExtractionOutput()),), 1
            )

        mid = len(batch) // 2
        logger.warning(
            "Extraction batch of %d window(s) for %s was rejected (%s); "
            "splitting into %d and %d and retrying.",
            len(batch),
            source_name,
            type(exc).__name__,
            mid,
            len(batch) - mid,
        )
        left = _extract_batch_splitting(
            agent, source_name=source_name, batch=batch[:mid]
        )
        right = _extract_batch_splitting(
            agent, source_name=source_name, batch=batch[mid:]
        )
        return _BatchOutcome(
            left.extractions + right.extractions,
            left.lost_windows + right.lost_windows,
        )


def _document_windows(chunks: tuple) -> tuple[tuple[int, str], ...]:
    """Flatten every chunk of a document into (chunk_index, window_text).

    Flattening across chunks is what lets a batch be full: batching within a
    chunk would leave a two-window chunk sending a batch of two and a
    one-window chunk sending a batch of one, which is most of the saving
    thrown away on short documents.
    """
    return tuple(
        (embedded.chunk.chunk_index, window)
        for embedded in chunks
        for window in _window_text(embedded.chunk.text, _WINDOW_CHARS, _WINDOW_OVERLAP)
    )


def extract_document(
    agent: ExtractionAgent,
    *,
    source_name: str,
    chunks: tuple,  # tuple[chunk_embed.types.EmbeddedChunk, ...]
) -> tuple[ChunkExtraction, ...]:
    """
    Run extraction across every chunk of a document.

    Windows from every chunk are flattened, grouped into batches, and each
    batch is one model call; the per-window results are then merged back into
    one ChunkExtraction per chunk. Extraction is still scoped per chunk --
    only the transport is shared.
    """
    windows = _document_windows(chunks)
    if not windows:
        return ()

    batches = [
        windows[i : i + _BATCH_WINDOWS] for i in range(0, len(windows), _BATCH_WINDOWS)
    ]
    logger.info(
        "Extracting %s: %d chunk(s), %d window(s), %d model call(s).",
        source_name,
        len(chunks),
        len(windows),
        len(batches),
    )

    per_window: list[ChunkExtraction] = []
    lost = 0
    for batch in batches:
        outcome = _extract_batch_splitting(
            agent, source_name=source_name, batch=batch
        )
        per_window.extend(outcome.extractions)
        lost += outcome.lost_windows

    if lost == len(windows):
        # Every window was rejected. Degrading to "extracted nothing" would
        # mark the job SUCCEEDED with an empty graph, which reads as a
        # document that simply had no facts in it -- the one failure mode
        # worse than failing.
        raise ExtractionFailed(
            f"every one of {len(windows)} window(s) of {source_name} was "
            f"rejected by the model"
        )
    if lost:
        logger.warning(
            "Extracted %s with %d of %d window(s) lost.",
            source_name,
            lost,
            len(windows),
        )

    # Back to one extraction per chunk, preserving the order the chunks came
    # in -- `_merge_windows` already knows how to fold several windows of one
    # chunk together.
    by_chunk: dict[int, list[ChunkExtraction]] = {}
    for extraction in per_window:
        by_chunk.setdefault(extraction.chunk_index, []).append(extraction)

    return tuple(
        _merge_windows(tuple(by_chunk[embedded.chunk.chunk_index]))
        for embedded in chunks
        if embedded.chunk.chunk_index in by_chunk
    )
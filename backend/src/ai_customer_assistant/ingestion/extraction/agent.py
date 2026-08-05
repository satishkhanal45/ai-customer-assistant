"""
EAV extraction agent (ingestion_flow.md step 5), implemented as a single
LangChain tool-calling completion per chunk -- not a multi-turn agentic
loop, since the task is structured extraction, not multi-step reasoning
with external actions. Bounding it to one call keeps cost/latency
predictable per chunk and keeps the whole thing a pure function of
(model, chunk) modulo the network call itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import SystemMessage, HumanMessage

from ingestion.extraction.prompts import (
    CHUNK_TASK_TEMPLATE,
    SYSTEM_PROMPT,
)
from ingestion.extraction.tools import (
    TOOL_DEFS,
    tool_calls_to_extraction,
)
from ingestion.pipeline_types import ChunkExtraction


class ToolCallingChatModel(Protocol):
    """Structural type for whatever chat model the app configures (see
    supervisor_plan.md's 'select the LLM provider and model' requirement)."""

    def bind_tools(self, tools: list[type]) -> "ToolCallingChatModel": ...

    def invoke(self, messages: list) -> object: ...  # returns an AIMessage


@dataclass(frozen=True, slots=True)
class ExtractionAgent:
    """A bound, ready-to-invoke model. Immutable -- build once, reuse."""

    model: ToolCallingChatModel


def build_extraction_agent(llm: ToolCallingChatModel) -> ExtractionAgent:
    return ExtractionAgent(model=llm.bind_tools(list(TOOL_DEFS)))


def _build_messages(*, source_name: str, chunk_index: int, chunk_text: str) -> list:
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=CHUNK_TASK_TEMPLATE.format(
                source_name=source_name,
                chunk_index=chunk_index,
                chunk_text=chunk_text,
            )
        ),
    ]


def extract_chunk(
    agent: ExtractionAgent,
    *,
    source_name: str,
    chunk_index: int,
    chunk_text: str,
) -> ChunkExtraction:
    """
    Run extraction for a single chunk. The only I/O is the one model call;
    everything before and after it is pure (see prompts.py / tools.py).
    """
    messages = _build_messages(
        source_name=source_name, chunk_index=chunk_index, chunk_text=chunk_text
    )
    response = agent.model.invoke(messages)
    tool_calls = getattr(response, "tool_calls", None) or []
    return tool_calls_to_extraction(chunk_index, tool_calls)


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

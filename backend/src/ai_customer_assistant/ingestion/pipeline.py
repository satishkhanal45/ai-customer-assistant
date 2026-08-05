"""
The end-to-end ingestion pipeline (ingestion_flow.md steps 3-8), wired as
a `Result`-composed sequence of async stages:

    fetch raw bytes (MinIO)
        -> extract text (Tika)
        -> chunk + embed (chunk_embed.pipeline.process_document)
        -> EAV extraction (LangChain tool-calling agent)
        -> persist chunks + entities/attributes/values/relations
        -> cutover (atomic) or mark FAILED

UPDATED against the real uploaded files:
  * everything is async (AsyncSession, StorageClient wrapped in asyncio.to_thread)
  * chunk_embed.types.ExtractedDocument is a DIFFERENT class from this
    package's ExtractedDocument (Tika's output) -- imported aliased below
  * process_document(document, *, settings, tokenizer, embedding_model,
    long_form_source_types, structured_source_types) has no reuse-by-checksum
    parameter, so chunk reuse is applied as a post-processing swap using
    queue.repository.previous_version_chunk_checksums's checksum->embedding map
  * tokenizer/embedding_model are loaded ONCE and injected via PipelineDeps,
    not reloaded per job

STILL TO CONFIRM (see integration notes): StorageClient construction
(needs StorageConfig, not included in what you sent), and where
IngestionSettings / tokenizer.get_tokenizer() / embedding.get_embedding_model()
actually live -- adjust `_resolve_deps` once confirmed.
"""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import UUID

import httpx
from langchain_groq import ChatGroq
from sqlalchemy.ext.asyncio import AsyncSession

from ingestion.chunk_embed.types import ExtractedDocument as ChunkerDocument
from ingestion.extraction.agent import ExtractionAgent, extract_document
from ingestion.pipeline_types import (
    ChunkExtraction,
    ExtractedDocument,
    FileType,
    IngestionContext,
    JobOutcome,
    JobRef,
    JobStatus,
    VersionStatus,
)
from ingestion.queue import repository as job_repo
from ingestion.result import Err, Ok, Result, run_pipeline
from ingestion.tika.client import (
    TikaExtractionError,
    TikaTransientError,
    extract_text,
)
from ingestion.tika.config import TikaSettings

FetchBytes = Callable[[str], Awaitable[bytes]]
ChunkAndEmbed = Callable[[ChunkerDocument], Awaitable[tuple]]  # -> tuple[EmbeddedChunk, ...]
PersistChunks = Callable[[AsyncSession, UUID, tuple, dict], Awaitable[tuple[str, ...]]]
PersistExtraction = Callable[[AsyncSession, UUID, tuple[ChunkExtraction, ...]], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class PipelineDeps:
    """Every I/O boundary the pipeline needs, injected as plain callables.
    This is what makes `run_ingestion` unit-testable without a real
    Postgres/MinIO/Tika/LLM stack: swap any field for a fake."""

    session: AsyncSession
    http_client: httpx.Client
    tika_settings: TikaSettings
    extraction_agent: ExtractionAgent
    fetch_raw_bytes: FetchBytes
    chunk_and_embed: ChunkAndEmbed
    persist_chunks: PersistChunks
    persist_extraction: PersistExtraction


# ---------------------------------------------------------------------------
# Stages. Each is `(deps) -> async (ctx) -> Result`, curried so `run_pipeline`
# can fold a tuple of stage callables regardless of deps.
# ---------------------------------------------------------------------------


def _stage_fetch_bytes(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        from ingestion.dedup import compute_checksum

        try:
            raw_bytes = await deps.fetch_raw_bytes(ctx.version.storage_uri)
        except Exception as exc:  # noqa: BLE001 - genuine I/O boundary
            return Err("storage_fetch_failed", str(exc))

        if compute_checksum(raw_bytes) != ctx.version.checksum:
            return Err("checksum_mismatch", "raw bytes do not match recorded checksum")

        return Ok(replace(ctx, raw_bytes=raw_bytes, content_type=ctx.version.mime_type))

    return stage


def _stage_extract_text(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        try:
            extracted: ExtractedDocument = extract_text(
                client=deps.http_client,
                settings=deps.tika_settings,
                raw_bytes=ctx.raw_bytes,
                content_type=ctx.content_type,
            )
        except TikaTransientError as exc:
            return Err("tika_transient", str(exc))
        except TikaExtractionError as exc:
            return Err("tika_extraction_failed", str(exc))

        return Ok(replace(ctx, extracted=extracted))

    return stage


def _stage_load_reuse_checksums(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        reuse_map = await job_repo.previous_version_chunk_checksums(
            deps.session, ctx.source.source_id, ctx.version.version_number
        )
        return Ok(replace(ctx, reuse_embeddings=reuse_map))

    return stage


def _stage_chunk_and_embed(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        chunker_document = ChunkerDocument(
            source_id=str(ctx.version.source_id),
            source_type=(ctx.version.file_type.value if ctx.version.file_type else "external_integration"),
            text=ctx.extracted.text,
            structure=None,
            metadata=ctx.extracted.metadata,
        )
        try:
            embedded_chunks = await deps.chunk_and_embed(chunker_document)
        except Exception as exc:  # noqa: BLE001
            return Err("chunk_embed_failed", str(exc))
        return Ok(replace(ctx, chunks=embedded_chunks))

    return stage


def _stage_eav_extraction(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        # LangChain's .invoke() is a sync network call; keep it off the
        # event loop so a slow completion doesn't block other jobs' I/O.
        try:
            extractions = await asyncio.to_thread(
                extract_document,
                deps.extraction_agent,
                source_name=ctx.source.source_name,
                chunks=ctx.chunks,
            )
        except Exception as exc:  # noqa: BLE001
            return Err("eav_extraction_failed", str(exc))
        return Ok(replace(ctx, chunk_extractions=extractions))

    return stage


def _stage_persist(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        try:
            await deps.persist_chunks(deps.session, ctx.version.version_id, ctx.chunks, ctx.reuse_embeddings)
            await deps.persist_extraction(deps.session, ctx.version.version_id, ctx.chunk_extractions)
        except Exception as exc:  # noqa: BLE001
            return Err("persist_failed", str(exc))
        return Ok(ctx)

    return stage


def _stage_cutover(deps: PipelineDeps) -> Callable[[IngestionContext], Awaitable[Result]]:
    async def stage(ctx: IngestionContext) -> Result:
        await job_repo.cutover(
            deps.session,
            source_id=ctx.source.source_id,
            new_version_id=ctx.version.version_id,
            old_version_id=ctx.source.current_version_id,
        )
        return Ok(ctx)

    return stage


_STAGE_BUILDERS = (
    _stage_fetch_bytes,
    _stage_extract_text,
    _stage_load_reuse_checksums,
    _stage_chunk_and_embed,
    _stage_eav_extraction,
    _stage_persist,
    _stage_cutover,
)


def _entities_created_count(extractions: tuple[ChunkExtraction, ...]) -> int:
    """Pure: distinct (entity_type, name) pairs resolved across the document."""
    return len({extraction.entity for extraction in extractions if extraction.entity is not None})


async def run_pipeline_async(initial: IngestionContext, stages: tuple) -> Result:
    """Async fold: functools.reduce can't await, so this walks the stage
    tuple directly, short-circuiting on the first Err -- same semantics as
    result.run_pipeline, just async-aware."""
    result: Result = Ok(initial)
    for stage in stages:
        if isinstance(result, Err):
            break
        result = await stage(result.value)
    return result


async def run_ingestion(session: AsyncSession, job: JobRef, deps: PipelineDeps | None = None) -> JobOutcome:
    """
    Handler registered for INITIAL_INGEST / REINDEX / UPDATE in the
    worker's dispatch table. Builds the initial context, runs every stage,
    and turns the final Result into a JobOutcome + version status update
    (steps 7a/7b).
    """
    source = await job_repo.load_source(session, job.source_id)
    version = await job_repo.load_version(session, job.version_id)
    deps = deps or await _resolve_deps(session)

    initial_ctx = IngestionContext(
        job=job, source=source, version=version, started_at=datetime.now(timezone.utc)
    )
    stages = tuple(builder(deps) for builder in _STAGE_BUILDERS)
    result = await run_pipeline_async(initial_ctx, stages)

    match result:
        case Ok(ctx):
            await job_repo.mark_version_status(session, version.version_id, VersionStatus.INDEXED)
            return JobOutcome(
                job_id=job.job_id,
                version_id=version.version_id,
                status=JobStatus.SUCCEEDED,
                chunks_created_count=len(ctx.chunks),
                entities_created_count=_entities_created_count(ctx.chunk_extractions),
            )
        case Err(reason, detail):
            await job_repo.mark_version_status(session, version.version_id, VersionStatus.FAILED)
            return JobOutcome(
                job_id=job.job_id,
                version_id=version.version_id,
                status=JobStatus.FAILED,
                error_details=f"{reason}: {detail}",
            )


async def run_delete(session: AsyncSession, job: JobRef) -> JobOutcome:
    """
    Handler for job_type = DELETE. Soft-deletes per schema.md's `is_active`
    flag rather than hard-deleting -- history is preserved.
    """
    from db.models import KnowledgeSource

    source = await session.get(KnowledgeSource, job.source_id)
    source.is_active = False
    source.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.commit()
    return JobOutcome(job_id=job.job_id, version_id=job.version_id, status=JobStatus.SUCCEEDED)


async def _resolve_deps(session: AsyncSession) -> PipelineDeps:
    """
    Wires real adapters. STILL NEEDS: StorageConfig (for StorageClient) and
    confirmation of IngestionSettings/tokenizer/embedding_model construction
    -- see INGESTION_INTEGRATION_NOTES.md.
    """
    from ingestion.storage.client import StorageClient, StorageConfig  # ASSUMPTION: needs StorageConfig
    from ingestion.chunk_embed.pipeline import process_document
    from ingestion.chunk_embed.tokenizer import get_tokenizer  # ASSUMPTION
    from ingestion.chunk_embed.embedding import get_embedding_model  # ASSUMPTION
    from ingestion.chunk_embed.config import IngestionSettings  # ASSUMPTION
    from ingestion.persistence import persist_chunk_extractions, persist_chunks

    storage_client = StorageClient(
        StorageConfig(
            endpoint=os.environ["MINIO_ENDPOINT"],
            access_key=os.environ["MINIO_ROOT_USER"],
            secret_key=os.environ["MINIO_ROOT_PASSWORD"],
            bucket_name=os.environ.get("MINIO_BUCKET", "knowledge-documents"),
            secure=(os.environ.get("MINIO_SECURE", "true") or "true").strip().lower()
            in {"1", "true", "yes", "on"},
        )
    )
    chunk_settings = IngestionSettings()
    tokenizer = get_tokenizer(chunk_settings.embedding_model_name)
    embedding_model = get_embedding_model(chunk_settings.embedding_model_name)

    async def fetch_raw_bytes(storage_uri: str) -> bytes:
        return await asyncio.to_thread(storage_client.get_object, storage_uri)

    async def chunk_and_embed(document: ChunkerDocument) -> tuple:
        return await asyncio.to_thread(
            process_document,
            document,
            settings=chunk_settings,
            tokenizer=tokenizer,
            embedding_model=embedding_model,
            long_form_source_types=frozenset({t.value for t in FileType} | {"external_integration"}),
            structured_source_types=frozenset(),
        )

    return PipelineDeps(
        session=session,
        http_client=httpx.Client(),
        tika_settings=TikaSettings(),
        extraction_agent=_default_extraction_agent(),
        fetch_raw_bytes=fetch_raw_bytes,
        chunk_and_embed=chunk_and_embed,
        persist_chunks=persist_chunks,
        persist_extraction=persist_chunk_extractions,
    )


def _default_extraction_agent() -> ExtractionAgent:
    """TODO: wire to the real LLM provider/model settings once config.py
    (sent empty) is filled in -- see INGESTION_INTEGRATION_NOTES.md."""
    from ingestion.extraction.agent import build_extraction_agent
    from langchain_groq import ChatGroq

    llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0)
    return build_extraction_agent(llm)

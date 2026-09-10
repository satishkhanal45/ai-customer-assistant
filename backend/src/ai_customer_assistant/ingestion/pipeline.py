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
  * the tokenizer and embedding model are loaded ONCE per process, by
    `build_pipeline_resources`, and injected via PipelineDeps -- see
    `PipelineResources` for why that needed saying twice
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ingestion.chunk_embed.types import ExtractedDocument as ChunkerDocument
from ingestion.extraction.agent import (
    ExtractionAgent,
    extract_document,
    is_rate_limited,
)
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
from timeouts import (
    INGEST_EXTRACTION_CALL_TIMEOUT_S,
    INGEST_EXTRACTION_MAX_RETRIES,
    INGEST_EXTRACTION_STAGE_BUDGET_S,
)

logger = logging.getLogger(__name__)

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
        # The provider call is synchronous; keep it off the event loop so a
        # slow completion doesn't block other I/O.
        #
        # Bounded on two axes, because they fail differently. The client's
        # own timeout (see `_default_extraction_agent`) bounds each HTTP
        # request and is what actually lets a stuck thread die. This budget
        # bounds the *document*: extraction makes one call per chunk, so a
        # long document can exceed any reasonable wall clock without a
        # single call being slow.
        #
        # Before either existed, one document held the worker for 28 minutes
        # with no output, and because the worker is serial the whole queue
        # stopped behind it.
        try:
            extractions = await asyncio.wait_for(
                asyncio.to_thread(
                    extract_document,
                    deps.extraction_agent,
                    source_name=ctx.source.source_name,
                    chunks=ctx.chunks,
                ),
                timeout=INGEST_EXTRACTION_STAGE_BUDGET_S,
            )
        except asyncio.TimeoutError:
            # Distinct from `eav_extraction_failed` so the Admin > Jobs page
            # can tell "the model refused this" from "this never came back".
            #
            # Honest limitation: `wait_for` abandons the await, but Python
            # cannot kill the worker thread, so the orphaned call keeps
            # running until the client's own timeout ends it. That is why
            # the client timeout is the primary bound and this is the
            # backstop -- the job is released either way, and the queue
            # keeps moving.
            return Err(
                "eav_extraction_timeout",
                f"extraction exceeded {INGEST_EXTRACTION_STAGE_BUDGET_S:.0f}s "
                f"for {len(ctx.chunks)} chunk(s)",
            )
        except Exception as exc:  # noqa: BLE001
            # Throttling and refusal arrive through the same `except` and
            # mean opposite things to the queue: the first passes on its own
            # once quota returns, the second will fail identically forever.
            # Splitting them here -- where the exception is still in hand --
            # is what lets the retry policy dispatch on a code instead of
            # re-parsing an error sentence two layers away.
            if is_rate_limited(exc):
                return Err("eav_extraction_rate_limited", str(exc))
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
        # Only now is `current_version_id` the version we just ingested, and
        # "current" is what decides which facts are still asserted. Running
        # this before the cutover would mark the incoming version's own facts
        # superseded, since it is not yet the current one.
        #
        # Deliberately not fatal. The facts are written and the document is
        # indexed; a currency pass that fails leaves stale values readable,
        # which is worse than nothing but far better than failing a job whose
        # real work succeeded -- and the pass is derived from scratch each
        # time, so the next ingestion of any document repairs it.
        try:
            from ingestion.persistence import resolve_superseded_values

            await resolve_superseded_values(deps.session)
            await deps.session.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "fact currency pass failed after cutover of version %s; "
                "superseded values may still read as current until the next "
                "ingestion",
                ctx.version.version_id,
                exc_info=True,
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
            # Discard everything the failed run staged before recording the
            # failure (P2-3). `_stage_persist` writes chunks and EAV rows with
            # add/flush and no commit of its own, so without this rollback the
            # `mark_version_status` call below -- which does commit -- would
            # carry those partial writes into the database alongside the
            # FAILED status. The retrieval join contract hides such orphans
            # from search, so nothing was visibly broken; they simply
            # accumulated, inflated the chunk counts, and made the tables
            # confusing to read directly.
            #
            # Safe to roll back here: every row this handler needs afterwards
            # (the job, source and version) was committed before the pipeline
            # started -- `claim_next_job` commits the RUNNING transition, and
            # the version row is created by the enqueuing API -- so
            # `mark_version_status` simply re-reads the version from the
            # database in the fresh transaction.
            await session.rollback()
            await job_repo.mark_version_status(session, version.version_id, VersionStatus.FAILED)
            return JobOutcome(
                job_id=job.job_id,
                version_id=version.version_id,
                status=JobStatus.FAILED,
                error_details=f"{reason}: {detail}",
                failure_kind=reason,
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


@dataclass(frozen=True, slots=True)
class PipelineResources:
    """Everything a pipeline run needs that does NOT depend on the session.

    The split exists because these are expensive and the session is not.
    `_resolve_deps` used to build all of it per job, which meant the 400 MB
    BGE model and its tokenizer were loaded from disk **once per document** --
    `get_tokenizer` and `get_embedding_model` both document that they are
    deliberately not memoized, leaving the caller to load once and inject,
    and the caller did not. The worker log showed `Loading weights: 199/199`
    on every single job.

    `pipeline.py`'s module docstring has claimed since it was written that
    "tokenizer/embedding_model are loaded ONCE and injected via PipelineDeps,
    not reloaded per job". This is the code that finally makes that true.

    The `httpx.Client` lives here for a second reason: the old one was
    created per job and never closed, leaking a file descriptor per
    document.
    """

    http_client: httpx.Client
    tika_settings: TikaSettings
    extraction_agent: ExtractionAgent
    fetch_raw_bytes: FetchBytes
    chunk_and_embed: ChunkAndEmbed


# One set per process. The worker runs one job at a time in a single event
# loop, so a plain module-level cache is enough and is visible in a way a
# hidden lru_cache on a loader is not. If concurrent jobs are ever added
# (ingestion.md item 12), note that a SentenceTransformer is not safe to
# encode from several threads at once -- that work needs a lock or a model
# per worker, not merely this cache.
_resources: PipelineResources | None = None


def build_pipeline_resources() -> PipelineResources:
    """Construct the expensive, session-independent half of the pipeline.

    Called once per process. Everything here either loads a model, opens a
    connection, or reads configuration -- none of it varies per job.
    """
    from ingestion.chunk_embed.config import IngestionSettings
    from ingestion.chunk_embed.embedding import get_embedding_model
    from ingestion.chunk_embed.pipeline import process_document
    from ingestion.chunk_embed.tokenizer import get_tokenizer
    from ingestion.storage.client import StorageClient, StorageConfig

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

    # One embedding model is shared by every lane of the worker, and a
    # `SentenceTransformer` is not safe to encode from several threads at
    # once -- which is exactly what concurrent lanes would do, since this runs
    # under `to_thread`. The alternative, a model per lane, costs 400 MB each
    # and is the thing hoisting the model to process scope was meant to avoid.
    #
    # Serialising encode is cheap here: a document's wall clock is dominated
    # by the extraction calls, and embedding is a small fraction of it. The
    # lock covers `to_thread` rather than living inside `process_document`
    # because the tokenizer is shared too.
    embed_lock = asyncio.Lock()

    async def chunk_and_embed(document: ChunkerDocument) -> tuple:
        async with embed_lock:
            return await asyncio.to_thread(
                process_document,
                document,
                settings=chunk_settings,
                tokenizer=tokenizer,
                embedding_model=embedding_model,
                long_form_source_types=frozenset(
                    {t.value for t in FileType} | {"external_integration"}
                ),
                structured_source_types=frozenset(),
            )

    return PipelineResources(
        http_client=httpx.Client(),
        tika_settings=TikaSettings(),
        extraction_agent=_default_extraction_agent(),
        fetch_raw_bytes=fetch_raw_bytes,
        chunk_and_embed=chunk_and_embed,
    )


def get_pipeline_resources() -> PipelineResources:
    """The process's resources, building them on first use."""
    global _resources
    if _resources is None:
        logger.info("Loading ingestion pipeline resources (embedding model, tokenizer).")
        _resources = build_pipeline_resources()
    return _resources


def reset_pipeline_resources() -> None:
    """Drop the cached resources, closing what they own.

    For process shutdown and for tests, which must not inherit a previous
    test's model or a closed HTTP client.
    """
    global _resources
    if _resources is not None:
        try:
            _resources.http_client.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            logger.debug("Closing the pipeline HTTP client failed.", exc_info=True)
    _resources = None


async def _resolve_deps(session: AsyncSession) -> PipelineDeps:
    """Bind this job's session to the process-wide resources.

    Cheap by construction: everything costly was built once by
    `get_pipeline_resources`, and all this does is attach the session and
    the two persistence callables.
    """
    from ingestion.persistence import persist_chunk_extractions, persist_chunks

    resources = get_pipeline_resources()
    return PipelineDeps(
        session=session,
        http_client=resources.http_client,
        tika_settings=resources.tika_settings,
        extraction_agent=resources.extraction_agent,
        fetch_raw_bytes=resources.fetch_raw_bytes,
        chunk_and_embed=resources.chunk_and_embed,
        persist_chunks=persist_chunks,
        persist_extraction=persist_chunk_extractions,
    )


def _default_extraction_agent() -> ExtractionAgent:
    """Wire the EAV extraction model. Honor EAV_MODEL when set; default to a
    Groq model in JSON mode.

    The key comes from `llm_credentials`, not straight from `os.environ`, so
    a key saved on the Admin > API Keys page reaches ingestion as well as
    chat. Reading the environment directly here meant the two disagreed:
    changing the key in the UI switched the assistant over and left the
    ingestion worker on the old one, with nothing anywhere saying so. The
    resolver still falls back to `GROQ_API_KEY`, so a deployment that never
    opens that page behaves exactly as before.
    """
    import os

    import llm_credentials
    from groq import Groq

    from ingestion.extraction.agent import build_extraction_agent

    model = os.environ.get("EAV_MODEL", "openai/gpt-oss-120b")
    # An explicit timeout, because the default is generous enough that a
    # hung request stalls the whole serial queue -- see
    # `_stage_eav_extraction`. `max_retries` keeps the SDK's own backoff,
    # which is what absorbed every 429 in the 2026-09-09 re-run, but bounds
    # it so the retries fit inside the call timeout instead of extending it.
    client = Groq(
        api_key=llm_credentials.api_key_for("groq"),
        timeout=INGEST_EXTRACTION_CALL_TIMEOUT_S,
        max_retries=INGEST_EXTRACTION_MAX_RETRIES,
    )
    return build_extraction_agent(client=client, model=model)

"""
Pipeline: ExtractedDocument -> tuple[EmbeddedChunk, ...].

Pure composition of chunking.chunk_document() and embedding.embed_chunks().
This module does not load the tokenizer or the embedding model itself —
per the explicit-dependency-injection pattern established in tokenizer.py
and embedding.py, the caller loads both exactly once (via
tokenizer.get_tokenizer() and embedding.get_embedding_model()) and passes
them into process_document() explicitly. This means many documents can be
processed in one run without reloading either the tokenizer or the model
for each one — the "load once" guarantee lives visibly in the caller's
code, not hidden behind a cache inside this module.

This module also does not know the real long_form_source_types /
structured_source_types values (still unconfirmed upstream, per
chunking.py) — callers must supply them explicitly.

Storage is intentionally NOT included here: process_document() stops at
tuple[EmbeddedChunk, ...]. Writing those to PostgreSQL + pgvector is
storage.py's responsibility, not this module's — keeping this pipeline
function's single responsibility to "produce embedded chunks," not
"produce and persist" them.
"""

from __future__ import annotations

from sentence_transformers import SentenceTransformer
from transformers.tokenization_utils_base import PreTrainedTokenizerBase

from ingestion.chunk_embed.chunking import PageRangeResolver, chunk_document
from ingestion.chunk_embed.config import IngestionSettings
from ingestion.chunk_embed.embedding import embed_chunks
from ingestion.chunk_embed.types import EmbeddedChunk, ExtractedDocument


def process_document(
    document: ExtractedDocument,
    *,
    settings: IngestionSettings,
    tokenizer: PreTrainedTokenizerBase,
    embedding_model: SentenceTransformer,
    long_form_source_types: frozenset[str],
    structured_source_types: frozenset[str],
    resolve_page_range: PageRangeResolver | None = None,
    embedding_batch_size: int | None = None,
) -> tuple[EmbeddedChunk, ...]:
    """
    Chunk ``document`` and generate embeddings for every resulting chunk.

    Args:
        document: The document to process.
        settings: Chunk size / overlap / embedding dimension / normalize
                   flag (see config.IngestionSettings). The embedding
                   model name in settings must match the model that
                   ``tokenizer`` and ``embedding_model`` were loaded from
                   — this function does not verify that itself.
        tokenizer: A loaded tokenizer instance (see
                   tokenizer.get_tokenizer), loaded once by the caller.
        embedding_model: A loaded SentenceTransformer instance (see
                          embedding.get_embedding_model), loaded once by
                          the caller.
        long_form_source_types: source_type values treated as long-form
                                 (see chunking.chunk_document).
        structured_source_types: source_type values treated as structured
                                  (see chunking.chunk_document).
        resolve_page_range: Optional page-boundary resolver, forwarded to
                             chunking.chunk_document() unchanged.
        embedding_batch_size: Optional batch size override, forwarded to
                               embedding.embed_chunks() unchanged.

    Returns:
        An immutable, document-ordered tuple of EmbeddedChunk. Empty if
        ``document`` produces no chunks (e.g. empty document text).

    Raises:
        chunking.UnknownSourceTypeError: if document.source_type is not
            found in exactly one of long_form_source_types /
            structured_source_types.
        embedding.EmptyChunkTextError: if chunking somehow produced a
            chunk with empty/whitespace-only text.
        embedding.EmbeddingGenerationError: if the embedding model fails.
        embedding.EmbeddingDimensionMismatchError: if a generated
            embedding's length does not match
            settings.embedding_dimension.

    Note:
        Exceptions from chunk_document() and embed_chunks() are not
        caught or wrapped here — this function's responsibility is
        composition, not error translation. Callers that need
        centralized error handling (e.g. logging, job-status updates)
        should catch these at the call site.
    """
    chunks = chunk_document(
        document,
        tokenizer=tokenizer,
        chunk_size_tokens=settings.chunk_size_tokens,
        chunk_overlap_tokens=settings.chunk_overlap_tokens,
        long_form_source_types=long_form_source_types,
        structured_source_types=structured_source_types,
        resolve_page_range=resolve_page_range,
    )

    return embed_chunks(
        chunks,
        model=embedding_model,
        normalize_embeddings=settings.normalize_embeddings,
        expected_dimension=settings.embedding_dimension,
        batch_size=embedding_batch_size,
    )


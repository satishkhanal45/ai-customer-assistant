"""
Embedding stage: tuple[Chunk, ...] -> tuple[EmbeddedChunk, ...].

Batched, not one-chunk-at-a-time: all chunk texts in a call are passed to
SentenceTransformer.encode() in one (internally batched) call, since
sentence-transformers batches natively for throughput and there's no
reason to give that up. sentence-transformers' encode() preserves input
order in its output even when it internally sorts by length for
efficiency, so EmbeddedChunk[i] always corresponds to chunks[i].

Consistent with tokenizer.py's explicit-injection pattern: this module
does NOT load the SentenceTransformer model itself. get_embedding_model()
loads it, uncached, and the caller (pipeline.py) is responsible for
loading it once per run and passing that single instance into
embed_chunks() explicitly.

Per config.py, embeddings are normalized (normalize_embeddings=True) so
that cosine similarity is meaningful at retrieval time — a decision this
module enforces by threading normalize_embeddings through to
SentenceTransformer.encode() rather than assuming a default.
"""

from __future__ import annotations

from sentence_transformers import SentenceTransformer

from ingestion.chunk_embed.types import Chunk, EmbeddedChunk


class EmbeddingError(Exception):
    """Base exception for this module."""


class EmptyChunkTextError(EmbeddingError):
    """Raised when a chunk with empty/whitespace-only text is passed in."""


class EmbeddingGenerationError(EmbeddingError):
    """
    Raised when the underlying embedding model fails to generate
    embeddings. Wraps the original exception rather than letting a raw
    library exception leak out of this module's public interface.
    """


class EmbeddingDimensionMismatchError(EmbeddingError):
    """
    Raised when a generated embedding's length does not match the
    configured embedding dimension (e.g. IngestionSettings.embedding_dimension,
    768). Storage requires VECTOR(768) exactly — a silent mismatch here
    would surface as a much more confusing error at the database layer.
    """


def get_embedding_model(model_name: str) -> SentenceTransformer:
    """
    Load the embedding model for ``model_name``.

    Not memoized: calling this twice loads the model twice. Callers
    (specifically pipeline.py) are responsible for loading it once per
    run and passing the resulting instance explicitly to embed_chunks(),
    per this project's explicit-dependency-injection preference over
    hidden/cached state.

    Args:
        model_name: e.g. "BAAI/bge-base-en-v1.5"
                    (see IngestionSettings.embedding_model_name).

    Returns:
        The loaded SentenceTransformer instance.
    """
    return SentenceTransformer(model_name)


def embed_chunks(
    chunks: tuple[Chunk, ...],
    *,
    model: SentenceTransformer,
    normalize_embeddings: bool,
    expected_dimension: int,
    batch_size: int | None = None,
) -> tuple[EmbeddedChunk, ...]:
    """
    Generate embeddings for ``chunks`` in a single batched call.

    Args:
        chunks: Chunks to embed, in document order. An empty tuple
                returns an empty tuple (no model call is made).
        model: A loaded SentenceTransformer instance (see
               get_embedding_model), passed in explicitly by the caller.
        normalize_embeddings: Whether to L2-normalize embeddings (see
                               IngestionSettings.normalize_embeddings —
                               required for correct cosine similarity at
                               retrieval time).
        expected_dimension: Required embedding vector length (see
                             IngestionSettings.embedding_dimension, 768).
                             Every generated embedding is validated
                             against this.
        batch_size: Optional override for SentenceTransformer's internal
                    batch size. When omitted, the library's own default
                    is used. This is a pass-through performance knob, not
                    a new business rule — added since sentence-transformers
                    already exposes it natively.

    Returns:
        An immutable tuple of EmbeddedChunk, in the same order as
        ``chunks`` (EmbeddedChunk[i].chunk is chunks[i]).

    Raises:
        EmptyChunkTextError: if any chunk has empty/whitespace-only text.
        EmbeddingGenerationError: if the underlying model fails to
            generate embeddings.
        EmbeddingDimensionMismatchError: if any generated embedding's
            length does not equal expected_dimension.
    """
    if not chunks:
        return ()

    _validate_non_empty_texts(chunks)

    texts = tuple(chunk.text for chunk in chunks)
    vectors = _generate_vectors(
        texts,
        model=model,
        normalize_embeddings=normalize_embeddings,
        batch_size=batch_size,
    )
    _validate_dimensions(vectors, expected_dimension)

    return tuple(
        EmbeddedChunk(chunk=chunk, embedding=vector)
        for chunk, vector in zip(chunks, vectors)
    )


def _validate_non_empty_texts(chunks: tuple[Chunk, ...]) -> None:
    """Reject any chunk whose text is empty or whitespace-only."""
    empty_chunk_indices = tuple(
        chunk.chunk_index for chunk in chunks if not chunk.text.strip()
    )
    if empty_chunk_indices:
        raise EmptyChunkTextError(
            f"Chunks with empty/whitespace-only text cannot be embedded "
            f"(chunk_index values: {empty_chunk_indices})"
        )


def _generate_vectors(
    texts: tuple[str, ...],
    *,
    model: SentenceTransformer,
    normalize_embeddings: bool,
    batch_size: int | None,
) -> tuple[tuple[float, ...], ...]:
    """
    Call the embedding model and return one immutable float tuple per
    input text, in input order.
    """
    encode_kwargs: dict[str, object] = {
        "normalize_embeddings": normalize_embeddings,
        "convert_to_numpy": True,
    }
    if batch_size is not None:
        encode_kwargs["batch_size"] = batch_size

    try:
        raw_vectors = model.encode(list(texts), **encode_kwargs)
    except Exception as exc:  # noqa: BLE001 - intentionally broad: wrap any
        # backend failure (OOM, corrupt weights, device error, etc.) into
        # this module's own exception type rather than leaking a raw
        # sentence-transformers/torch exception to callers.
        raise EmbeddingGenerationError(
            f"Embedding generation failed for {len(texts)} chunk(s): {exc}"
        ) from exc

    return tuple(tuple(float(component) for component in vector) for vector in raw_vectors)


def _validate_dimensions(
    vectors: tuple[tuple[float, ...], ...], expected_dimension: int
) -> None:
    """Ensure every generated vector has exactly expected_dimension components."""
    mismatched = tuple(
        (index, len(vector))
        for index, vector in enumerate(vectors)
        if len(vector) != expected_dimension
    )
    if mismatched:
        raise EmbeddingDimensionMismatchError(
            f"Expected every embedding to have dimension {expected_dimension}, "
            f"but got mismatches (index, actual_dimension): {mismatched}"
        )


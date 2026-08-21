"""Shared BGE embedding singleton (Phase 6, §4.6).

Per the integration plan, `embed_query` (Knowledge Agent's vector search)
and the shared embedding model must share ONE ``SentenceTransformer``
(BAAI/bge-base-en-v1.5) instance, constructed once in ``chat_service`` and
injected everywhere — never re-instantiated per request.

This module owns that construction: ``build_shared_embeddings`` loads the
model once (module-level cache, so a process constructs exactly one), and
hands back both consumption faces:

  - ``embed_query(text) -> tuple[float, ...]`` — the
    ``EmbeddingFunction`` the Knowledge Agent's ``build_knowledge_agent_graph``
    injects into its ``vector_search`` node;
  - ``embedding_model`` — the raw ``SentenceTransformer`` instance.

Tests inject a fake model via ``build_shared_embeddings(model=...)`` so no
network/model download is needed; the server path loads the real model once
at startup.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional, Sequence

from sentence_transformers import SentenceTransformer

from agents.knowledge.constants import DEFAULT_EMBEDDING_MODEL_NAME


class SharedEmbeddings:
    """One BGE instance with its two consumption faces.

    ``embed_query`` is derived from ``model``, so both consumers always
    share the exact same weights and normalization behaviour. The raw
    model is kept on the instance for callers that need a
    ``SentenceTransformer`` directly.
    """

    def __init__(self, model: SentenceTransformer) -> None:
        self._model = model

    @property
    def embedding_model(self) -> SentenceTransformer:
        return self._model

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Embed a single query, normalized, as the Knowledge Agent's
        vector search expects (cosine similarity over normalized vectors)."""
        vector = self._model.encode(
            [text], normalize_embeddings=True, convert_to_numpy=True
        )
        return tuple(float(component) for component in vector[0])


@lru_cache(maxsize=1)
def _cached_model(model_name: str) -> SentenceTransformer:
    from ingestion.chunk_embed.embedding import get_embedding_model

    return get_embedding_model(model_name)


def build_shared_embeddings(
    model: Optional[SentenceTransformer] = None,
    model_name: Optional[str] = None,
) -> SharedEmbeddings:
    """Construct the process-wide shared embeddings.

    Passing ``model`` bypasses loading entirely (tests/fakes). Otherwise
    the BGE model is loaded once and cached for the process lifetime.
    ``model_name`` defaults to the Knowledge Agent config's
    ``embedding_model_name``.
    """
    resolved_name = model_name or DEFAULT_EMBEDDING_MODEL_NAME
    resolved_model = model or _cached_model(resolved_name)
    return SharedEmbeddings(resolved_model)
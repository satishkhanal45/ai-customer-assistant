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

import os
from functools import lru_cache
from typing import Optional, Sequence

from sentence_transformers import SentenceTransformer

from agents.knowledge.constants import BGE_QUERY_INSTRUCTION, DEFAULT_EMBEDDING_MODEL_NAME

# Env override for the query-side instruction (P2-2). Set it to a different
# string to use another model's convention, or to the empty string to encode
# queries bare — which is what this project did before, and is what you want
# if you ever A/B the change against an existing index.
_QUERY_INSTRUCTION_ENV_VAR = "EMBEDDING_QUERY_INSTRUCTION"


def resolve_query_instruction(model_name: str) -> str:
    """Pure: the instruction to prepend to queries for ``model_name``.

    Resolution order — explicit env override, then the model family's own
    documented convention, then nothing. Only the `bge-*-en-v1.5` family gets
    an automatic instruction: `bge-m3` and the `e5` models use different
    prefixes, and applying the wrong one is worse than applying none, so an
    unrecognised model is left bare rather than guessed at.
    """
    override = os.environ.get(_QUERY_INSTRUCTION_ENV_VAR)
    if override is not None:
        return override
    normalized = model_name.lower()
    if "bge-" in normalized and normalized.endswith("-en-v1.5"):
        return BGE_QUERY_INSTRUCTION
    return ""


class SharedEmbeddings:
    """One BGE instance with its two consumption faces.

    ``embed_query`` is derived from ``model``, so both consumers always
    share the exact same weights and normalization behaviour. The raw
    model is kept on the instance for callers that need a
    ``SentenceTransformer`` directly.

    ``query_instruction`` is prepended to every query before encoding, and
    to nothing else. That asymmetry is the point: `bge-*-en-v1.5` is trained
    for asymmetric retrieval, where passages are encoded bare and queries
    carry a fixed instruction. Encoding both sides bare — what this class
    used to do — put queries and passages under a different convention than
    the one the weights were trained on. Since only the query side changes,
    the existing index stays valid; no re-embedding is required.
    """

    def __init__(
        self,
        model: SentenceTransformer,
        query_instruction: str = "",
    ) -> None:
        self._model = model
        self._query_instruction = query_instruction

    @property
    def embedding_model(self) -> SentenceTransformer:
        return self._model

    @property
    def query_instruction(self) -> str:
        return self._query_instruction

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Embed a single query, normalized, as the Knowledge Agent's
        vector search expects (cosine similarity over normalized vectors)."""
        vector = self._model.encode(
            [self._query_instruction + text],
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return tuple(float(component) for component in vector[0])


@lru_cache(maxsize=1)
def _cached_model(model_name: str) -> SentenceTransformer:
    from ingestion.chunk_embed.embedding import get_embedding_model

    return get_embedding_model(model_name)


def build_shared_embeddings(
    model: Optional[SentenceTransformer] = None,
    model_name: Optional[str] = None,
    query_instruction: Optional[str] = None,
) -> SharedEmbeddings:
    """Construct the process-wide shared embeddings.

    Passing ``model`` bypasses loading entirely (tests/fakes). Otherwise
    the BGE model is loaded once and cached for the process lifetime.
    ``model_name`` defaults to the Knowledge Agent config's
    ``embedding_model_name``.

    ``query_instruction`` overrides the query-side prefix; leave it None to
    take it from ``resolve_query_instruction`` (env var, else the model
    family's documented convention). Pass ``""`` to disable it explicitly.
    It is resolved from ``model_name``, not from an injected ``model``, so a
    test passing a fake model gets no instruction unless it asks for one.
    """
    resolved_name = model_name or DEFAULT_EMBEDDING_MODEL_NAME
    resolved_model = model or _cached_model(resolved_name)
    resolved_instruction = (
        query_instruction
        if query_instruction is not None
        else resolve_query_instruction(resolved_name)
    )
    return SharedEmbeddings(resolved_model, query_instruction=resolved_instruction)

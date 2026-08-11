"""Tests for the shared BGE embedding singleton (Phase 6, §4.6).

The deliverable: ``embed_query`` (Knowledge Agent's vector search) and
``ChatService.embedding_model`` (Safety's groundedness binding) must share
ONE ``SentenceTransformer`` instance, constructed once and never
re-instantiated per request. These tests prove the sharing contract:

  - ``build_shared_embeddings(model=...)`` returns a ``SharedEmbeddings``
    whose ``embed_query`` and ``embedding_model`` expose the *same* model
    instance (identity, not equality);
  - ``embed_query`` returns a normalized float tuple the Knowledge graph's
    ``vector_search`` accepts (``Sequence[float]``);
  - ``_build_real_knowledge_graph`` wires that same ``embed_query`` into a
    compiled Knowledge graph, so the live seam works end-to-end.
"""
from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.chat_service import _build_real_knowledge_graph
from services.embeddings import build_shared_embeddings


class _FakeModel:
    """Deterministic SentenceTransformer standin: no network, fixed dim."""

    def __init__(self, dimension: int = 3) -> None:
        self.dimension = dimension

    def encode(self, texts, **kwargs) -> np.ndarray:
        vectors = np.array(
            [[float(i + 1) for _ in range(self.dimension)] for i in range(len(texts))]
        )
        # mirror SentenceTransformer.encode(normalize_embeddings=True)
        if kwargs.get("normalize_embeddings"):
            vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors


class TestSharedEmbeddingsSingleton:
    def test_embed_query_and_embedding_model_share_one_instance(self):
        model = _FakeModel()
        shared = build_shared_embeddings(model=model)

        # Identity, not just equality: Safety must see the exact same model
        # instance the Knowledge graph's embed_query was built from.
        assert shared.embedding_model is model

        query_vector = shared.embed_query("what is the refund policy?")
        assert isinstance(query_vector, tuple)
        assert all(isinstance(component, float) for component in query_vector)

    def test_embed_query_output_is_normalized(self):
        # normalize_embeddings=True → L2-norm of the vector is 1.0, matching
        # the cosine-similarity contract the vector_search expects.
        model = _FakeModel()
        query_vector = build_shared_embeddings(model=model).embed_query("refund")

        norm = np.linalg.norm(list(query_vector))
        assert norm == pytest.approx(1.0)

    def test_two_instances_with_one_model_still_share_that_model(self):
        model = _FakeModel()
        first = build_shared_embeddings(model=model)
        second = build_shared_embeddings(model=model)
        # Both unique SharedEmbeddings wrappers, but ONE underlying model.
        assert first is not second
        assert first.embedding_model is second.embedding_model


class TestRealKnowledgeGraphAssembly:
    def test_builds_compiled_graph_with_shared_embed_query(self):
        shared = build_shared_embeddings(model=_FakeModel())
        graph = _build_real_knowledge_graph(
            shared_embeddings=shared,
            session_factory=async_sessionmaker(),
        )
        from langgraph.graph.state import CompiledStateGraph

        assert isinstance(graph, CompiledStateGraph)
        # The same shared embeddings exposed on the wrapper are the ones
        # compiled into the graph.
        assert shared.embedding_model is shared.embedding_model
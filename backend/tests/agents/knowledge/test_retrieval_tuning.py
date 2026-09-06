"""Retrieval quality: the BGE query instruction and the relative cutoff (P2-2).

Two untuned things shipped together, and they interact, which is why they are
tested together:

1. **The query side had no instruction.** `bge-*-en-v1.5` is trained for
   asymmetric retrieval — passages bare, queries prefixed with a fixed
   instruction. Both sides were encoded bare, so every query was embedded
   under a convention the weights were never trained on. Measured against the
   live corpus by `scripts/calibrate_retrieval.py`, adding it moved recall@1
   from 76.9% to 92.3% and MRR from 0.885 to 0.955.

2. **The only filter was an absolute floor, set to 0.70 with nothing behind
   the number.** The same measurement showed 0.70 kept the correct source for
   just 5 of 26 answerable questions — and for most of the 21 it rejected, the
   correct document was ranked *first*. The system had the answer and refused
   to use it.

The instruction also shifts the whole score distribution down (median top
score 0.681 -> 0.637), so it could not be adopted without moving the floor.
`test_threshold_is_below_the_measured_positive_floor` is what stops someone
re-raising the threshold and silently undoing the recall fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.knowledge import vector_search
from agents.knowledge.config import KnowledgeAgentConfig
from agents.knowledge.constants import (
    BGE_QUERY_INSTRUCTION,
    DEFAULT_RELATIVE_SCORE_MARGIN,
    DEFAULT_SIMILARITY_THRESHOLD,
)
from services.embeddings import SharedEmbeddings, build_shared_embeddings, resolve_query_instruction

GOLDEN_SET = Path(__file__).resolve().parents[2] / "data" / "golden_retrieval.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN_SET.read_text())


class _RecordingModel:
    """Captures exactly what text reaches `encode`, which is the whole point:
    the instruction must reach queries and nothing else."""

    def __init__(self) -> None:
        self.encoded: list[list[str]] = []

    def encode(self, texts, **kwargs):
        self.encoded.append(list(texts))
        return [[0.1] * 768 for _ in texts]


class TestQueryInstruction:
    def test_query_is_prefixed_with_the_instruction(self):
        model = _RecordingModel()
        embeddings = SharedEmbeddings(model, query_instruction=BGE_QUERY_INSTRUCTION)

        embeddings.embed_query("what are your rates?")

        assert model.encoded == [[BGE_QUERY_INSTRUCTION + "what are your rates?"]]

    def test_no_instruction_means_the_query_is_encoded_bare(self):
        model = _RecordingModel()
        SharedEmbeddings(model, query_instruction="").embed_query("hello")
        assert model.encoded == [["hello"]]

    def test_index_time_embedding_stays_bare(self):
        """The asymmetry is the whole mechanism. If ingestion ever starts
        prefixing chunk text too, the query instruction stops helping and the
        existing index silently becomes wrong."""
        source = (
            Path(vector_search.__file__).resolve().parents[2]
            / "ingestion"
            / "chunk_embed"
            / "embedding.py"
        ).read_text()
        assert "texts = tuple(chunk.text for chunk in chunks)" in source
        assert BGE_QUERY_INSTRUCTION not in source

    def test_bge_en_v15_gets_the_documented_instruction(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
        assert resolve_query_instruction("BAAI/bge-base-en-v1.5") == BGE_QUERY_INSTRUCTION
        assert resolve_query_instruction("BAAI/bge-large-en-v1.5") == BGE_QUERY_INSTRUCTION

    def test_an_unrecognised_model_is_left_bare(self, monkeypatch):
        """`bge-m3` and the `e5` family use different prefixes. Guessing one
        is worse than using none, so unknown models get nothing."""
        monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
        assert resolve_query_instruction("BAAI/bge-m3") == ""
        assert resolve_query_instruction("intfloat/e5-large-v2") == ""
        assert resolve_query_instruction("sentence-transformers/all-MiniLM-L6-v2") == ""

    def test_env_override_wins_and_empty_disables(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCTION", "custom: ")
        assert resolve_query_instruction("BAAI/bge-base-en-v1.5") == "custom: "
        monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCTION", "")
        assert resolve_query_instruction("BAAI/bge-base-en-v1.5") == ""

    def test_builder_resolves_the_instruction_for_the_real_model_name(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
        built = build_shared_embeddings(model=_RecordingModel())
        assert built.query_instruction == BGE_QUERY_INSTRUCTION

    def test_explicit_empty_instruction_is_honoured(self):
        built = build_shared_embeddings(model=_RecordingModel(), query_instruction="")
        assert built.query_instruction == ""


class _Row:
    """Minimal stand-in for the SQLAlchemy Row the ranking helpers see."""

    def __init__(self, chunk_id: str) -> None:
        self.chunk_id = chunk_id


class TestRelativeMargin:
    def test_results_far_below_the_best_hit_are_dropped(self):
        ranked = [(_Row("a"), 0.83), (_Row("b"), 0.80), (_Row("c"), 0.66)]
        kept = vector_search._apply_relative_margin(ranked, relative_score_margin=0.12)
        assert [row.chunk_id for row, _ in kept] == ["a", "b"]

    def test_a_flat_result_set_is_kept_whole(self):
        """When the top hit is only 0.71, a 0.66 chunk is in the same
        conversation — which an absolute floor cannot express."""
        ranked = [(_Row("a"), 0.71), (_Row("b"), 0.68), (_Row("c"), 0.66)]
        kept = vector_search._apply_relative_margin(ranked, relative_score_margin=0.12)
        assert len(kept) == 3

    def test_zero_margin_disables_the_filter(self):
        ranked = [(_Row("a"), 0.90), (_Row("b"), 0.10)]
        assert vector_search._apply_relative_margin(ranked, relative_score_margin=0.0) == tuple(ranked)

    def test_empty_input_is_safe(self):
        assert vector_search._apply_relative_margin([], relative_score_margin=0.12) == ()

    def test_the_best_hit_is_never_dropped(self):
        ranked = [(_Row("a"), 0.51)]
        kept = vector_search._apply_relative_margin(ranked, relative_score_margin=0.9)
        assert len(kept) == 1

    def test_python_ranking_path_applies_the_margin(self):
        """The two ranking paths must stay equivalent, so the margin has to be
        wired into both, not just the one that is easiest to reach."""
        import json as _json

        class _EmbeddingRow:
            def __init__(self, chunk_id, vector):
                self.chunk_id = chunk_id
                self.embedding = _json.dumps(vector)

        query = [1.0, 0.0]
        rows = [
            _EmbeddingRow("near", [1.0, 0.0]),          # similarity 1.00
            _EmbeddingRow("mid", [0.97, 0.24]),          # ~0.97
            _EmbeddingRow("far", [0.71, 0.71]),          # ~0.71
        ]
        ranked = vector_search._rank_by_similarity(
            rows, tuple(query), similarity_threshold=0.5, top_k=8, relative_score_margin=0.1
        )
        assert [row.chunk_id for row, _ in ranked] == ["near", "mid"]


class TestCalibratedDefaults:
    def test_threshold_is_below_the_measured_positive_floor(self):
        """Measured on the live corpus, the *lowest* top score across 26
        answerable questions was 0.480 and the highest across 10 unanswerable
        ones was 0.557. The floor has to sit low enough to keep answerable
        questions; raising it back toward 0.70 re-breaks 21 of 26."""
        assert DEFAULT_SIMILARITY_THRESHOLD <= 0.55
        assert DEFAULT_SIMILARITY_THRESHOLD >= 0.35, "a floor this low rejects nothing at all"

    def test_relative_margin_is_enabled_by_default(self):
        """With a permissive floor, the margin is what keeps the context
        clean — 0.12 halved the mean result set with no recall cost."""
        assert 0.0 < DEFAULT_RELATIVE_SCORE_MARGIN <= 0.2

    def test_config_exposes_the_margin_and_validates_it(self):
        assert KnowledgeAgentConfig().relative_score_margin == DEFAULT_RELATIVE_SCORE_MARGIN
        assert KnowledgeAgentConfig(relative_score_margin=0.0).relative_score_margin == 0.0
        with pytest.raises(ValueError):
            KnowledgeAgentConfig(relative_score_margin=1.5)


class TestGoldenSetIsUsable:
    """The golden set is the evidence behind every number above, so it has to
    stay well-formed even though the calibration script itself needs a live
    database and is not run in CI."""

    def test_it_has_both_populations(self, golden):
        assert len(golden["positives"]) >= 20
        assert len(golden["negatives"]) >= 5, (
            "without unanswerable questions the floor cannot be measured at all — "
            "recall alone is maximised by a threshold of zero"
        )

    def test_every_positive_names_its_evidence(self, golden):
        for item in golden["positives"]:
            assert item["query"].strip()
            assert item["expected_sources"], f"no expected source for {item['query']!r}"

    def test_queries_are_unique(self, golden):
        queries = [i["query"] for i in golden["positives"]] + [
            i["query"] for i in golden["negatives"]
        ]
        assert len(queries) == len(set(queries))

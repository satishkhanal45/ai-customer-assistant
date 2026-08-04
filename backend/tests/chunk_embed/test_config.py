"""Unit tests for ingestion/config.py — IngestionSettings."""

from __future__ import annotations

import pytest

from ingestion.chunk_embed.config import IngestionSettings


class TestDefaults:
    def test_confirmed_default_values(self) -> None:
        settings = IngestionSettings(_env_file=None)
        assert settings.chunk_size_tokens == 500
        assert settings.chunk_overlap_tokens == 75
        assert settings.embedding_model_name == "BAAI/bge-base-en-v1.5"
        assert settings.embedding_dimension == 768
        assert settings.normalize_embeddings is True

    def test_overlap_ratio_matches_confirmed_15_percent(self) -> None:
        settings = IngestionSettings(_env_file=None)
        assert settings.overlap_ratio == pytest.approx(0.15)


class TestEnvironmentOverride:
    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGESTION_CHUNK_SIZE_TOKENS", "600")
        monkeypatch.setenv("INGESTION_CHUNK_OVERLAP_TOKENS", "90")
        settings = IngestionSettings(_env_file=None)
        assert settings.chunk_size_tokens == 600
        assert settings.chunk_overlap_tokens == 90

    def test_unset_env_vars_fall_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INGESTION_CHUNK_SIZE_TOKENS", raising=False)
        settings = IngestionSettings(_env_file=None)
        assert settings.chunk_size_tokens == 500


class TestValidation:
    def test_overlap_equal_to_chunk_size_is_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: PT011 - pydantic wraps ValueError; exact type may vary by version
            IngestionSettings(_env_file=None, chunk_size_tokens=500, chunk_overlap_tokens=500)

    def test_overlap_greater_than_chunk_size_is_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: PT011
            IngestionSettings(_env_file=None, chunk_size_tokens=500, chunk_overlap_tokens=600)

    def test_negative_overlap_is_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: PT011
            IngestionSettings(_env_file=None, chunk_size_tokens=500, chunk_overlap_tokens=-1)

    def test_zero_overlap_is_valid(self) -> None:
        settings = IngestionSettings(_env_file=None, chunk_size_tokens=500, chunk_overlap_tokens=0)
        assert settings.chunk_overlap_tokens == 0


class TestImmutability:
    def test_settings_are_frozen(self) -> None:
        settings = IngestionSettings(_env_file=None)
        with pytest.raises(Exception):  # noqa: PT011 - pydantic's frozen-model exception type may vary by version
            settings.chunk_size_tokens = 999  # type: ignore[misc]


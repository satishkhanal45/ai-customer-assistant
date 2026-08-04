"""
Unit tests for ingestion/tokenizer.py.

get_tokenizer() is tested via monkeypatch against transformers.AutoTokenizer,
so no real model is ever downloaded. encode/decode/count_tokens are tested
against the WordTokenizer test double (see conftest.py), which is
sufficient since these functions only depend on the tokenizer exposing
encode()/decode() — the same interface a real HuggingFace tokenizer has.
"""

from __future__ import annotations

import pytest

from ingestion.chunk_embed import tokenizer as tokenizer_module
from ingestion.chunk_embed.tokenizer import count_tokens, decode, encode, get_tokenizer


class _RecordingFakeTokenizer:
    """Minimal stand-in returned by a monkeypatched AutoTokenizer.from_pretrained."""


class TestGetTokenizer:
    def test_delegates_to_auto_tokenizer_from_pretrained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        class FakeAutoTokenizer:
            @staticmethod
            def from_pretrained(model_name: str) -> _RecordingFakeTokenizer:
                calls.append(model_name)
                return _RecordingFakeTokenizer()

        monkeypatch.setattr(tokenizer_module, "AutoTokenizer", FakeAutoTokenizer)

        result = get_tokenizer("BAAI/bge-base-en-v1.5")

        assert calls == ["BAAI/bge-base-en-v1.5"]
        assert isinstance(result, _RecordingFakeTokenizer)

    def test_not_memoized_loads_fresh_instance_each_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0

        class FakeAutoTokenizer:
            @staticmethod
            def from_pretrained(model_name: str) -> object:
                nonlocal call_count
                call_count += 1
                return object()

        monkeypatch.setattr(tokenizer_module, "AutoTokenizer", FakeAutoTokenizer)

        first = get_tokenizer("BAAI/bge-base-en-v1.5")
        second = get_tokenizer("BAAI/bge-base-en-v1.5")

        assert call_count == 2
        assert first is not second


class TestEncode:
    def test_returns_tuple_of_token_ids(self, word_tokenizer) -> None:
        result = encode("hello world", word_tokenizer)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_excludes_special_tokens(self, word_tokenizer) -> None:
        seen_kwargs = {}
        original_encode = word_tokenizer.encode

        def spy_encode(text: str, add_special_tokens: bool = False):
            seen_kwargs["add_special_tokens"] = add_special_tokens
            return original_encode(text, add_special_tokens=add_special_tokens)

        word_tokenizer.encode = spy_encode
        encode("hello world", word_tokenizer)
        assert seen_kwargs["add_special_tokens"] is False

    def test_empty_text_returns_empty_tuple(self, word_tokenizer) -> None:
        assert encode("", word_tokenizer) == ()


class TestDecode:
    def test_round_trip_with_encode(self, word_tokenizer) -> None:
        original = "hello world foo"
        token_ids = encode(original, word_tokenizer)
        assert decode(token_ids, word_tokenizer) == original

    def test_empty_token_tuple_returns_empty_string(self, word_tokenizer) -> None:
        assert decode((), word_tokenizer) == ""


class TestCountTokens:
    def test_matches_length_of_encode(self, word_tokenizer) -> None:
        text = "one two three four"
        assert count_tokens(text, word_tokenizer) == len(encode(text, word_tokenizer))

    def test_empty_text_is_zero(self, word_tokenizer) -> None:
        assert count_tokens("", word_tokenizer) == 0

    def test_unicode_text(self, word_tokenizer) -> None:
        text = "héllo wörld 你好 emoji😀test"
        # Should not raise, and should count each whitespace-separated token.
        assert count_tokens(text, word_tokenizer) == len(text.split())


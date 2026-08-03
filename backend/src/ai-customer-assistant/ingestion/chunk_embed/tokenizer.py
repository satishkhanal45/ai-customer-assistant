"""
Shared tokenizer access for the chunking / embedding pipeline.

Loads the BGE tokenizer directly via transformers.AutoTokenizer, rather
than through a full SentenceTransformer instance, so that chunking.py can
count and window tokens without needing to load the full embedding model.
embedding.py separately loads its own SentenceTransformer for vector
generation (see embedding.py) — the two are not required to share one
model instance, only to reference the same model name from config.py, so
token counts here match what the embedding model will actually consume.

All functions in this module are pure with respect to their arguments:
they take a tokenizer and text/tokens in, and return a new value out,
never mutating their inputs. This module holds no cached or hidden
state: get_tokenizer() loads a fresh instance on every call, by design.
Loading once and reusing that single instance across a run is the
caller's responsibility — pipeline.py loads the tokenizer exactly once
and passes it explicitly into every function that needs it, so the
"load once" guarantee is visible in the call chain rather than hidden
behind a cache inside this module.
"""

from __future__ import annotations

from transformers import AutoTokenizer
from transformers.tokenization_utils_base import PreTrainedTokenizerBase


def get_tokenizer(model_name: str) -> PreTrainedTokenizerBase:
    """
    Load the tokenizer for ``model_name``.

    No caching is performed here: each call loads a fresh instance.
    Callers that need to reuse a single instance across many operations
    (e.g. pipeline.py, across an entire document run) must load it once
    and pass that instance explicitly to every function that needs it.

    Args:
        model_name: HuggingFace model identifier, e.g. "BAAI/bge-base-en-v1.5"
                    (see IngestionSettings.embedding_model_name).

    Returns:
        A newly loaded tokenizer instance.
    """
    return AutoTokenizer.from_pretrained(model_name)


def encode(text: str, tokenizer: PreTrainedTokenizerBase) -> tuple[int, ...]:
    """
    Tokenize ``text`` into token ids, excluding special tokens
    (e.g. [CLS]/[SEP]) since those are added automatically by the
    embedding model at inference time and are not part of the content
    being measured for chunk-size/overlap purposes.

    Args:
        text: The text to tokenize.
        tokenizer: A tokenizer instance, as returned by get_tokenizer().

    Returns:
        An immutable tuple of token ids, in order.
    """
    return tuple(tokenizer.encode(text, add_special_tokens=False))


def decode(token_ids: tuple[int, ...], tokenizer: PreTrainedTokenizerBase) -> str:
    """
    Reconstruct text from token ids produced by encode().

    Used by the recursive splitter in chunking.py to turn a token-index
    window back into the chunk's text content.

    Args:
        token_ids: Token ids, as returned by encode().
        tokenizer: A tokenizer instance, as returned by get_tokenizer().

    Returns:
        The decoded text for the given token ids.
    """
    return tokenizer.decode(list(token_ids), skip_special_tokens=True)


def count_tokens(text: str, tokenizer: PreTrainedTokenizerBase) -> int:
    """
    Count the number of content tokens in ``text``, excluding special
    tokens, using the same convention as encode().

    Args:
        text: The text to measure.
        tokenizer: A tokenizer instance, as returned by get_tokenizer().

    Returns:
        The token count.
    """
    return len(encode(text, tokenizer))

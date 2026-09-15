"""Folding text differences that are not differences.

Lives at the package root rather than under `ingestion` because both sides of
the write path need it: `db.models.Value` derives its comparison key from it,
and `ingestion.persistence` sets that key explicitly on its Core inserts.
Putting it under `ingestion` would mean `db.models` importing from a package
that imports `db.models`.
"""

from __future__ import annotations

import re
import unicodedata

_DASHES = re.compile(r"[‐-―−]")
_SINGLE_QUOTES = re.compile(r"[‘’‛′]")
_DOUBLE_QUOTES = re.compile(r"[“”″]")
_WHITESPACE = re.compile(r"\s+")


def normalize_value(value: str) -> str:
    """Fold the differences between two values that are not differences.

    Case, surrounding whitespace, runs of whitespace, and the Unicode
    punctuation variants a model emits interchangeably. Deliberately *not*
    folded: digits, separators inside numbers, and word order -- "4,000 -
    7,000" and "4000-7000" stay distinct here, because a rule loose enough
    to merge those is loose enough to merge two genuinely different prices,
    and this deployment has a pair of those too ("0-8000" against
    "4,000 - 8,000").

    The result is a comparison key, never displayed. The original text is
    what a customer sees.
    """
    folded = unicodedata.normalize("NFKC", value)
    folded = _DASHES.sub("-", folded)
    folded = _SINGLE_QUOTES.sub("'", folded)
    folded = _DOUBLE_QUOTES.sub('"', folded)
    folded = _WHITESPACE.sub(" ", folded).strip()
    return folded.casefold()



def normalize_value(value: str) -> str:
    """Fold the differences between two values that are not differences.

    Case, surrounding whitespace, runs of whitespace, and the Unicode
    punctuation variants a model emits interchangeably. Deliberately *not*
    folded: digits, separators inside numbers, and word order -- "4,000 -
    7,000" and "4000-7000" stay distinct, because a rule loose enough to
    merge those is loose enough to merge two genuinely different prices, and
    this deployment has a pair of those ("0-8000" against "4,000 - 8,000").

    The result is a comparison key, never displayed. The original text is
    what a customer sees.
    """
    folded = unicodedata.normalize("NFKC", value)
    folded = _DASHES.sub("-", folded)
    folded = _SINGLE_QUOTES.sub("'", folded)
    folded = _DOUBLE_QUOTES.sub('"', folded)
    folded = _WHITESPACE.sub(" ", folded).strip()
    return folded.casefold()

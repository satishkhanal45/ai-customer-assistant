"""Deciding when two extracted values are the same fact twice.

The corpus accumulated values like these under one entity and attribute:

    MVP / definition
      "functional version of the product that includes all the basic
       features required to solve the problem while it is not polished ..."
      "functional version of the product that includes all the basic
       features required to solve the problem while not polished ..."
      "smallest yet fun"
      "smallest yet functional version of the product that includes only
       the essential features required to solve the problem"

They are stored as four separate rows because `value` is unique on the exact
text, so anything short of a byte-identical repeat is a new fact as far as
the database is concerned. Retrieval then returns all four as though the
document had said four different things.

Three causes, and they need different fixes:

1. **Text that differs invisibly** -- `PHP Intern` and `php intern`,
   `pre-defined` with an ASCII hyphen and with U+2011. `normalize_value`
   folds these, and a unique index on the normalized form makes the
   duplicate unrepresentable rather than relying on every writer to behave.
2. **The model paraphrasing the same fact** across two overlapping windows --
   "while it is not polished" against "while not polished".
   `collapse_near_duplicates` merges these.
3. **Windows cut mid-word**, which is where "smallest yet fun" comes from:
   a window boundary landed inside "functional" and the model dutifully
   extracted the fragment. That one is fixed at the source, in
   `extraction.agent._window_text`, because no amount of comparing values
   afterwards recovers the half of the sentence that was never sent.

The hard constraint on all of this is that plenty of attributes genuinely
take several values -- `Agile / stage` holds six real stages, and
`Alpinist Studios / objective` six real objectives. Collapsing those would
turn a correct answer into a lossy one, which is worse than the duplicates.
So the rules here are deliberately conservative: they merge only what is
demonstrably one fact written twice.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from text_normalization import normalize_value

__all__ = [
    "NEAR_DUPLICATE_RATIO",
    "collapse_near_duplicates",
    "normalize_value",
]

# Unicode has many characters that read as a hyphen or a quote and compare
# unequal. NFKC folds some of them and leaves others -- U+2011 NON-BREAKING
# HYPHEN normalizes to U+2010 HYPHEN, which is still not the ASCII the rest
# of the corpus uses -- so the classes are mapped explicitly afterwards.
# How alike two values must be before one is treated as a restatement of the
# other. Measured against this corpus rather than picked:
#
#   0.980  "... while it is not polished ..." / "... while not polished ..."
#   0.950  "internal testing, validation, and refinement" / "... validation,
#          refinement"
#   0.943  "... before full development" / "... before full development phase"
#   ---- threshold ----
#   0.905  "linear and step-by-step" / "linear step-by-step"      (a real
#          duplicate this misses)
#   0.893  "https://alpiniststudios.com/" / ".../about"           (two URLs)
#   0.706  "0-8000" / "4,000 - 8,000"                             (two prices)
#   0.703  "Mobile App Development" / "Web Development"           (two answers)
#
# The band just below the line is where real duplicates and real distinctions
# sit together, so the line goes above it. Missing a duplicate leaves a
# redundant row; merging two prices deletes an answer.
NEAR_DUPLICATE_RATIO = 0.92


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def collapse_near_duplicates(values: list[str]) -> list[str]:
    """Drop values that restate another value in the list.

    Returns the survivors in their original order. When two values are near
    duplicates the **longer** one wins: the shorter is usually the truncated
    or less specific telling of the same fact ("... before full development"
    against "... before full development phase"), and the longer one carries
    strictly more of the answer.

    Comparison happens on the normalized form, so casing and punctuation do
    not stop two restatements from being recognised as one.
    """
    if len(values) < 2:
        return list(values)

    # Longest first, so the value that survives a comparison is already the
    # one we would keep and the loop never has to reconsider a decision.
    ranked = sorted(values, key=lambda v: (-len(v), v))
    kept: list[str] = []
    for candidate in ranked:
        normalized = normalize_value(candidate)
        if any(
            _similarity(normalized, normalize_value(k)) >= NEAR_DUPLICATE_RATIO
            for k in kept
        ):
            continue
        kept.append(candidate)

    survivors = set(kept)
    return [v for v in values if v in survivors]

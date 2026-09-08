"""
deduplication.py — remove duplicated information between structured
facts and document chunks.

`deduplicate()` is a pure function operating in three passes, each
addressing a distinct kind of duplication:

1. Exact-duplicate structured facts (same entity + attribute + value,
   which can happen if a general entity-values lookup and a
   attribute-specific lookup both ran and overlapped).
2. Exact-duplicate retrieved chunks (same chunk_id — defensive; the
   two hybrid-fan-out arms query disjoint tables so this shouldn't
   occur in practice, but a re-run or retry path could produce it).
Both passes preserve input order (assumed to already be ranked by
ranking.py) and keep the *first* occurrence of a duplicate key, so
deduplication never silently promotes a lower-ranked duplicate over a
higher-ranked one.

That used to be done with a `dict` + `reversed()` trick, and the trick did
not actually hold. A dict built from the reversed input orders its keys by
each key's *last* occurrence, so reversing the values back does not restore
the original order once a duplicate exists: `[high, low, high]` came out as
`[low, high]`, promoting the lower-ranked chunk to the front — exactly what
the comment claimed was impossible. The explicit seen-set below is longer by
three lines and is obviously correct by reading.

## The cross-type redundancy pass, and why it is gone (F9)

A third pass dropped any retrieved chunk whose text contained both a
structured fact's value and that fact's entity label, on the reasoning that
"the chunk saying the same thing in prose adds no new information and just
spends context budget".

That reasoning is wrong, and it cost the customer a correct answer. Traced
live for *"Why did Soani Tech change its name?"*:

    retrieved:  1 chunk, 2463 chars, similarity 0.663 -- the press release
    ranked:     1 chunk
    deduped:    0 chunks
    prompt:     "No relevant documentation was found for this query."
    answer:     "I'm sorry, but I don't have information on the reason..."

The refusal was *correct*: the documentation section really was empty. The
chunk had been discarded because the graph held the relation fact
`Alpinist Studios formerly_known_as "Soani Tech"`, and the press release
mentions both names -- as any document explaining a rebrand must.

So the "both must match" safeguard fails in exactly the case where it
matters most: the chunk is dropped precisely *because* it is the document
about the relationship the fact records. The fact said two names are
related; the chunk carried the reasons, a CEO quote, and what changed. Two
thousand characters were thrown away to save the tokens of one.

The pass is removed rather than tightened, because there was never much to
tighten toward. A retrieved chunk is a passage, not a restatement -- the
"chunk that merely repeats a fact" case is close to hypothetical, while the
failure above is real and was silent. Context budget is already governed by
`config.max_context_chunks` and by vector_search's relative score margin,
and the two kinds of material land in separate prompt sections with the
answer prompt telling the model which to prefer on conflict.

Worth knowing: F5 widened the blast radius shortly before this was found.
Making `hybrid` the near-universal strategy means facts and chunks now
coexist on almost every turn, where a structured-only or vector-only route
would previously have carried just one of the two -- so a pass that fires
only when both are present went from occasional to routine.
"""

from __future__ import annotations

from typing import Callable, Iterable

from .types import RankedResult, RetrievedChunk, StructuredFact


def deduplicate(result: RankedResult) -> RankedResult:
    """Return a new RankedResult with exact-duplicate facts and exact-
    duplicate chunks removed.

    A structured fact never causes a chunk to be discarded -- see the module
    docstring for the answer that cost.
    """
    return RankedResult(
        structured_facts=_dedupe_facts(result.structured_facts),
        retrieved_chunks=_dedupe_chunks(result.retrieved_chunks),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _first_by_key(items: Iterable, key: Callable) -> tuple:
    """Pure: keep the first item for each key, in input order."""
    seen: set = set()
    kept = []
    for item in items:
        item_key = key(item)
        if item_key not in seen:
            seen.add(item_key)
            kept.append(item)
    return tuple(kept)


def _dedupe_facts(facts: tuple[StructuredFact, ...]) -> tuple[StructuredFact, ...]:
    """Keyed by (entity_id, attribute, value) — a general entity-values
    lookup and an attribute-specific one can both return the same fact."""
    return _first_by_key(facts, lambda fact: (fact.entity_id, fact.attribute, fact.value))


def _dedupe_chunks(chunks: tuple[RetrievedChunk, ...]) -> tuple[RetrievedChunk, ...]:
    return _first_by_key(chunks, lambda chunk: chunk.chunk_id)

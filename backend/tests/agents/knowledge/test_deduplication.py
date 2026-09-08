"""A structured fact must never delete a document chunk (F9).

`deduplicate` had a third pass that dropped any retrieved chunk whose text
contained both a structured fact's value and that fact's entity label, on the
reasoning that "the chunk saying the same thing in prose adds no new
information and just spends context budget".

Traced live for *"Why did Soani Tech change its name?"*:

    retrieved:  1 chunk, 2463 chars, similarity 0.663 — the press release
    ranked:     1 chunk
    deduped:    0 chunks
    prompt:     "No relevant documentation was found for this query."
    answer:     "I'm sorry, but I don't have information on the reason..."

The refusal was *correct* — the documentation section really was empty. The
chunk had been discarded because the graph held the relation fact
`Alpinist Studios formerly_known_as "Soani Tech"`, and a press release about
a rebrand necessarily mentions both names.

So the "both must match" safeguard failed in exactly the case where it
mattered most: the chunk was dropped *because* it was the document about the
relationship the fact recorded. The fact said two names are related; the
chunk carried the reasons and a CEO quote explaining them.

The module had no tests at all, which is how it could delete answers in
silence for as long as it did.
"""
from __future__ import annotations

from agents.knowledge.deduplication import deduplicate
from agents.knowledge.types import (
    ChunkProvenance,
    RankedResult,
    RetrievedChunk,
    StructuredFact,
)

# The press release, shortened but keeping what mattered: both company names,
# and the sentence the customer was asking for.
PRESS_RELEASE = (
    "soani tech, a leading kathmandu-based software development company, "
    "proudly announces its rebranding to alpinist studios. this new identity "
    "reflects the company's evolution, global outlook, and commitment to "
    "helping clients scale new heights in digital innovation. \"this rebrand "
    "is more than just a name change. it's a reflection of who we've become "
    "and where we're headed,\" said john chhetri, ceo of alpinist studios."
)


def _chunk(text: str = PRESS_RELEASE, chunk_id: str = "c1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        chunk_text=text,
        similarity_score=0.663,
        provenance=ChunkProvenance(
            source_name="soani-tech-is-now-alpinist-studios",
            source_type="EXTERNAL_INTEGRATION",
            category_name=None,
            version_number=1,
            page=None,
            chunk_index=0,
            entity_type=None,
            entity_label=None,
        ),
    )


def _fact(
    attribute: str = "",
    value: str = "Soani Tech",
    entity_label: str = "Alpinist Studios",
    relation_type: str | None = "formerly_known_as",
    entity_id: str = "e1",
) -> StructuredFact:
    return StructuredFact(
        entity_id=entity_id,
        entity_type="Company",
        entity_label=entity_label,
        attribute=attribute,
        value=value,
        value_type="string",
        relation_type=relation_type,
        related_entity_label=value if relation_type else None,
        confidence=1.0,
    )


class TestTheAnswerThatWasDeleted:
    def test_the_press_release_survives_the_relation_fact(self):
        """The exact combination that shipped: a rebrand document, and the
        relation fact recording the rebrand."""
        result = deduplicate(
            RankedResult(structured_facts=(_fact(),), retrieved_chunks=(_chunk(),))
        )
        assert len(result.retrieved_chunks) == 1
        assert "more than just a name change" in result.retrieved_chunks[0].chunk_text

    def test_a_chunk_naming_both_sides_of_a_fact_is_kept(self):
        """A document that mentions a fact's value *and* its entity is the
        document explaining the relationship — the most relevant thing in the
        corpus, and precisely what used to be discarded."""
        chunk = _chunk(text="alpinist studios was formerly known as soani tech, because ...")
        result = deduplicate(
            RankedResult(structured_facts=(_fact(),), retrieved_chunks=(chunk,))
        )
        assert result.retrieved_chunks == (chunk,)

    def test_facts_never_reduce_the_chunk_count(self):
        """Stated as an invariant, because the failure was silent: no
        combination of facts may shrink the documentation the model sees."""
        chunks = (_chunk(chunk_id="a"), _chunk(chunk_id="b", text="unrelated prose"))
        facts = (
            _fact(),
            _fact(value="Kathmandu", relation_type=None, attribute="founded_location"),
            _fact(value="Alpinist Studios", entity_label="Soani Tech"),
        )
        result = deduplicate(RankedResult(structured_facts=facts, retrieved_chunks=chunks))
        assert len(result.retrieved_chunks) == len(chunks)


class TestExactDuplicatesAreStillRemoved:
    """The two passes that were always sound are kept — they remove genuine
    duplicates rather than deciding one kind of material outranks another."""

    def test_the_same_chunk_twice_becomes_one(self):
        chunk = _chunk(chunk_id="c1")
        result = deduplicate(
            RankedResult(structured_facts=(), retrieved_chunks=(chunk, chunk))
        )
        assert len(result.retrieved_chunks) == 1

    def test_the_same_fact_twice_becomes_one(self):
        fact = _fact()
        result = deduplicate(
            RankedResult(structured_facts=(fact, fact), retrieved_chunks=())
        )
        assert len(result.structured_facts) == 1

    def test_distinct_facts_are_all_kept(self):
        facts = (_fact(), _fact(value="Kathmandu", attribute="founded_location", relation_type=None))
        result = deduplicate(RankedResult(structured_facts=facts, retrieved_chunks=()))
        assert len(result.structured_facts) == 2

    def test_rank_order_survives(self):
        """Deduplication must never promote a lower-ranked duplicate over a
        higher-ranked one."""
        first, second = _chunk(chunk_id="high"), _chunk(chunk_id="low")
        result = deduplicate(
            RankedResult(structured_facts=(), retrieved_chunks=(first, second, first))
        )
        assert [c.chunk_id for c in result.retrieved_chunks] == ["high", "low"]

    def test_empty_input_is_safe(self):
        result = deduplicate(RankedResult(structured_facts=(), retrieved_chunks=()))
        assert result.structured_facts == ()
        assert result.retrieved_chunks == ()


def test_the_cross_type_pass_is_gone():
    """A grep-style guard. Reinstating it would silently delete answers again,
    and nothing else in the suite would notice."""
    from agents.knowledge import deduplication

    assert not hasattr(deduplication, "_is_redundant_with_facts")

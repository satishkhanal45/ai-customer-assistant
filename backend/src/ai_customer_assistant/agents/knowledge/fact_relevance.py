"""
fact_relevance.py — decide which structured facts earn a place in the prompt.

## The defect this exists to prevent (F4)

`structured_lookup` has three shapes, chosen by what extraction asked for. Two
are precise — "this attribute", "this relation". The third, `general`, fires
when neither was requested, and it returns **everything the graph knows about
the entity**. That dump then goes into the prompt under the heading
"Structured Facts", ahead of the retrieved documentation, and the answer
prompt tells the model to *prefer* it:

    4. If Structured Facts and Relevant Documentation conflict, prefer the
       Structured Facts (they come from an exact, deterministic record)

Measured live, on "What are the core values of the company?":

    extraction:  entity_type='Company', confidence=0.62   (no slot requested)
    strategy:    hybrid
    structured:  15 facts, every one of them about Agile delivery practices
                 - Company "Company" uses "Sprint Planning"
                 - Company "Company" uses "Daily Collaboration"
                 - Company "Company" uses "Retrospective"
                 ... ten more of the same shape
    documentation: contained "core value" and "integrity" — the actual answer

The same question, three runs:

    facts=0   chunks=4  ->  grounded, correct answer
    facts=15  chunks=4  ->  NOT grounded, "I don't have information..."
    facts=0   chunks=4  ->  grounded, correct answer

**The run that retrieved more was the run that failed.** An authoritative-
looking section that answers nothing reads as evidence of absence, and the
model stops looking at the documentation directly below it.

## The rule

A *slotted* lookup is trustworthy: the customer asked for an attribute or a
relation, and whatever came back is the answer to that. Those facts pass
through untouched.

A *general* lookup is a dump, and is kept only where it overlaps the question.
Overlap is computed against the fact's attribute, value, relation and related
entity — deliberately **not** its entity type or label, because the entity was
already matched by the lookup. Including it would make every fact "relevant"
to any question mentioning the entity, which is exactly the case that broke.

Dropping too much is the safer error here, and that asymmetry is measured
rather than assumed: a surviving irrelevant fact demonstrably causes a
refusal, while a dropped useful one still leaves the documentation section —
which is where the answer to a broad question lives anyway. When a general
lookup is filtered down to nothing during the structured-only strategy, the
P1-6 fallback then runs a semantic search instead, which is a better outcome
than either.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .types import StructuredFact, StructuredQuery

# Terms carrying no discriminating power. Deliberately short: this is a
# tie-breaker between facts about one already-resolved entity, not a search
# engine, so an over-eager stoplist would do more harm than an under-eager one.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "were", "our", "your", "their", "its",
        "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
        "does", "did", "can", "could", "would", "should", "will", "shall",
        "have", "has", "had", "been", "being", "with", "from", "into", "that",
        "this", "these", "those", "there", "here", "any", "all", "you", "yours",
        "about", "tell", "give", "show", "list", "please", "need", "want",
        "company", "companies", "organisation", "organization", "business",
    }
)

# Below this, a token is noise ("of", "in", "a") or too generic to match on.
_MIN_TERM_LENGTH = 3

# Two terms that are not identical after plural-normalisation still count as
# the same word when they share this much leading text — "orchestrate" /
# "orchestration", "deliver" / "delivery". Five is deliberately conservative:
# four would let "complete" match "complex". This is a coarse relevance check
# between facts about one already-resolved entity, not a search engine, and a
# real stemmer would be more machinery than the job needs.
_MIN_SHARED_PREFIX = 5


def _terms(text: str | None) -> frozenset[str]:
    """Pure: content terms in ``text``, lowercased and stopworded."""
    if not text:
        return frozenset()
    tokens = re.split(r"[^0-9a-zA-Z]+", text.lower())
    return frozenset(
        token
        for token in tokens
        if len(token) >= _MIN_TERM_LENGTH and token not in _STOPWORDS
    )


def _singular(term: str) -> str:
    """Pure: a crude plural fold, so "values" and "value" are one term.

    Only two endings, and deliberately not a third: an "es" rule looks
    tempting ("boxes" -> "box") but folds "uses" to "us", losing the single
    commonest match there is. Stripping the bare "s" gets "uses" -> "use",
    "values" -> "value" and "practices" -> "practice" right, and the
    shared-prefix rule below covers what it misses.
    """
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith("s") and len(term) > 3:
        return term[:-1]
    return term


def _shared_prefix_length(left: str, right: str) -> int:
    """Pure: how much leading text two terms have in common."""
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def _matches(left: str, right: str) -> bool:
    """Pure: do two terms refer to the same thing, allowing inflection?"""
    left, right = _singular(left), _singular(right)
    if left == right:
        return True
    return _shared_prefix_length(left, right) >= _MIN_SHARED_PREFIX


def fact_terms(fact: StructuredFact) -> frozenset[str]:
    """Pure: the terms a fact should be matched on.

    ``entity_type`` and ``entity_label`` are excluded on purpose — the lookup
    already matched the entity, so including them would make every fact about
    it match any question that names it. That is precisely the failure this
    module exists to prevent.
    """
    return frozenset().union(
        _terms(fact.attribute),
        _terms(fact.value),
        _terms(fact.relation_type),
        _terms(fact.related_entity_label),
    )


def is_relevant(fact: StructuredFact, query_terms: Iterable[str]) -> bool:
    """Pure: does this fact share any content term with the question?"""
    candidate_terms = fact_terms(fact)
    return any(
        _matches(query_term, candidate)
        for query_term in query_terms
        for candidate in candidate_terms
    )


def is_slotted(query: StructuredQuery | None) -> bool:
    """Pure: did extraction ask for a specific attribute or relation?

    A slotted lookup returns an answer to a question that was actually asked.
    An unslotted one returns everything known about the entity.
    """
    if query is None:
        return False
    return query.attribute is not None or query.relation_type is not None


def relevant_facts(
    facts: Sequence[StructuredFact],
    *,
    query: StructuredQuery | None,
    query_text: str | None,
) -> tuple[StructuredFact, ...]:
    """Pure: the facts worth showing the model, given what was asked.

    Slotted lookups pass through untouched. Unfiltered entity dumps are kept
    only where they overlap the question — see the module docstring for the
    measurement behind that.

    Degrades to passing everything through whenever it cannot do better: no
    query, no query text, or a question made entirely of stopwords. Silently
    discarding facts because the *filter* had nothing to work with would be a
    worse failure than the one it is preventing.
    """
    if is_slotted(query):
        return tuple(facts)

    query_terms = _terms(query_text)
    if not query_terms:
        return tuple(facts)

    return tuple(fact for fact in facts if is_relevant(fact, query_terms))

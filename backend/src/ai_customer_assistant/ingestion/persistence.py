"""
The I/O boundary for steps 4-6 of ingestion_flow.md: writing chunks, and
resolving/writing the EAV facts an extraction produced for them.

UPDATED: rewritten async, against the real ORM models (note the column is
EmbeddingChunk.text, not chunk_text), and taking chunk_embed's own
EmbeddedChunk objects directly instead of a local ChunkDraft -- process_document
has no reuse-by-checksum concept, so that substitution happens here via the
`reused_embeddings` map (checksum -> embedding) computed by
queue.repository.previous_version_chunk_checksums.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Attribute,
    Entity,
    EmbeddingChunk,
    KnowledgeSource,
    KnowledgeSourceEntityMap,
    Relation,
    Value,
    ValueProvenance,
)
from ingestion.extraction.ontology import safe_canonicalize_entity_type
from ingestion.pipeline_types import ChunkExtraction
from ingestion.values import collapse_near_duplicates, normalize_value

logger = logging.getLogger(__name__)


def compute_chunk_checksum(text: str) -> str:
    """Pure: EmbeddedChunk has no checksum field, so we derive one here."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def persist_chunks(
    session: AsyncSession,
    version_id: UUID,
    embedded_chunks: tuple,  # tuple[chunk_embed.types.EmbeddedChunk, ...]
    reused_embeddings: dict[str, tuple[float, ...]],
) -> tuple[str, ...]:
    """Replace this version's chunks. entity_id starts NULL for all of them
    (step 4); _link_entity_to_chunk sets it once extraction (step 5)
    resolves a real entity. Returns the checksums written, in order.

    **Replace, not append.** This used to `add()` unconditionally, so
    ingesting a version twice wrote every chunk twice -- and the lookup by
    (version_id, chunk_index) in `_link_entity_to_chunk` then failed with
    "Multiple rows were found when exactly one was required". 31 of the 82
    jobs in this database failed that way, and the state was permanent:
    once a version held duplicates, every later attempt failed identically,
    so the document could never be ingested again.

    Deleting first loses nothing. Chunks belong to a *version*, and a
    version is an immutable snapshot -- `_stage_fetch_bytes` verifies the
    bytes still match the version's recorded checksum before any of this
    runs, and fails with `checksum_mismatch` if they do not. So the rows
    being deleted were derived from the same bytes as the rows replacing
    them. Superseded versions have different `version_id`s and are
    untouched; `cutover` only marks them STALE.

    (Whether those superseded versions should remain *searchable* is a
    separate, open question -- status.md P1-7.)
    """
    # Same transaction as the insert below, so a failure mid-write leaves
    # the previous chunks intact rather than deleting them and stopping.
    await session.execute(
        delete(EmbeddingChunk).where(EmbeddingChunk.version_id == version_id)
    )
    await session.flush()

    checksums: list[str] = []
    for embedded in embedded_chunks:  # one INSERT per row, needs its own values
        checksum = compute_chunk_checksum(embedded.chunk.text)
        embedding = reused_embeddings.get(checksum, embedded.embedding)
        session.add(
            EmbeddingChunk(
                version_id=version_id,
                entity_id=None,
                chunk_index=embedded.chunk.chunk_index,
                text=embedded.chunk.text,
                embedding=list(embedding),
                page=(embedded.chunk.metadata or {}).get("page"),
                token_count=embedded.chunk.token_count,
                checksum=checksum,
            )
        )
        checksums.append(checksum)
    await session.flush()
    return tuple(checksums)


async def _resolve_entity(session: AsyncSession, entity_type: str, name: str) -> UUID:
    """Resolve-or-create an entity. **A name is one entity, whatever its type.**

    Identity used to include the type, and that fragmented the graph. The
    schema is unique on ``(entity_type, name)``, the ontology passes types it
    does not recognise through unchanged, and the extraction prompt actively
    invites the model to invent a type when none of the canonical ones fit --
    so every invented type minted a new entity. "Agile" ended up stored three
    times, as ``Methodology``, ``Process`` and ``Development Process``,
    holding 39, 14 and 2 facts: 55 facts about one concept, split across three
    identities the database considered unrelated.

    The error underneath it is that ``entity_type`` is an *attribute*, not an
    identity discriminator. "Python is a Programming Language" and "Python is
    a Technology" are both true, and neither makes it a different Python.

    So the lookup is by normalized name alone. All 62 fragmented names in this
    corpus were the same real-world thing seen through different lenses --
    supertype/subtype pairs (``Technology`` over ``Library``, ``Phase`` over
    ``SDLC Phase``) or facets (Instagram is a Company *and* a Platform *and* a
    Product) -- with no genuine homonyms among them.

    The caveat that comes with that: a true homonym *would* now merge. In a
    single-company knowledge base that is a remote risk and a visible one (the
    facts contradict each other), where fragmentation was certain and silent.

    Canonicalizing the type still matters for the row this creates, and the
    ``(entity_type, name)`` upsert remains the final guarantee. Keeping the raw
    ``name`` as the display value means identity is normalized without changing
    what anyone sees.
    """
    canonical_type = safe_canonicalize_entity_type(entity_type)
    normalized_name = name.strip()

    existing = (
        await session.execute(
            select(Entity.id)
            .where(func.lower(Entity.name) == normalized_name.lower())
            .order_by(Entity.created_at, Entity.id)
            .limit(1)
        )
    ).scalars().all()
    if existing:
        # Oldest row wins, so the identity a document resolves to does not
        # depend on which type the model happened to emit this time.
        return existing[0]

    stmt = (
        pg_insert(Entity)
        .values(label=normalized_name, entity_type=canonical_type, name=normalized_name)
        .on_conflict_do_update(index_elements=[Entity.entity_type, Entity.name], set_={"name": normalized_name})
        .returning(Entity.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def _resolve_attribute(
    session: AsyncSession, namespace: str, name: str, value_type: str, multivalue: bool
) -> UUID:
    stmt = (
        pg_insert(Attribute)
        .values(namespace=namespace, name=name, value_type=value_type, multivalue=multivalue)
        .on_conflict_do_update(index_elements=[Attribute.namespace, Attribute.name], set_={"value_type": value_type})
        .returning(Attribute.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def _link_entity_to_chunk(session: AsyncSession, version_id: UUID, chunk_index: int, entity_id: UUID) -> None:
    """Point one chunk at the entity its extraction resolved.

    `scalar_one()` used to be the lookup here, which turned two data
    problems into the same unhelpful crash. Duplicate chunks now cannot
    exist -- `persist_chunks` replaces rather than appends, and
    `uq_chunk_version_index` enforces it -- so the remaining case is a
    *missing* chunk, which happens when the model returns an index the
    document does not have. That is the extraction being wrong about one
    chunk, not a reason to fail the whole document, so it is skipped with a
    warning that names the version and the index rather than raising
    "Multiple rows were found when exactly one was required" from three
    frames away.
    """
    chunk = (
        await session.execute(
            select(EmbeddingChunk).where(
                EmbeddingChunk.version_id == version_id,
                EmbeddingChunk.chunk_index == chunk_index,
            )
        )
    ).scalars().first()

    if chunk is None:
        logger.warning(
            "Extraction referenced chunk_index %s of version %s, which does "
            "not exist; skipping the entity link for it.",
            chunk_index,
            version_id,
        )
        return

    chunk.entity_id = entity_id
    session.add(KnowledgeSourceEntityMap(version_id=version_id, entity_id=entity_id, relationship_type="DERIVED_CHUNK"))


async def _persist_one_extraction(
    session: AsyncSession,
    version_id: UUID,
    extraction: ChunkExtraction,
    document: "_DocumentFacts | None" = None,
) -> UUID | None:
    """Write one chunk's resolved entity + facts + relations. Returns the
    resolved entity_id, or None if there was no primary entity.

    Facts and relations are persisted even when no primary ``resolve_entity``
    call was made: the model often emits ``record_attribute_value`` /
    ``record_relation`` for entities it never resolved, and those must not be
    dropped. Each fact/relation resolves (and, if needed, creates) its own
    referenced entities."""
    entity_id: UUID | None = None

    resolved: dict[tuple[str, str], UUID] = {}

    async def resolve(entity_type_: str, name_: str) -> UUID:
        key = (entity_type_, name_)
        if key not in resolved:
            resolved[key] = await _resolve_entity(session, entity_type_, name_)
        return resolved[key]

    if extraction.entity is not None:
        entity_type, name = extraction.entity
        entity_id = await resolve(entity_type, name)
        await _link_entity_to_chunk(session, version_id, extraction.chunk_index, entity_id)

    document = document or _DocumentFacts(surviving={}, multivalued=frozenset())

    for fact in extraction.facts:
        fact_key = (
            fact.entity_type,
            fact.entity_name,
            fact.namespace,
            fact.attribute_name,
        )
        # Another window of this document already said this, in slightly
        # different words. Writing it again would store one fact twice.
        if not document.keeps(fact_key, fact.value):
            continue

        attribute_id = await _resolve_attribute(
            session,
            fact.namespace,
            fact.attribute_name,
            fact.value_type,
            # `multivalue` arrived hardcoded False from the extractor, so every
            # attribute in the database claimed to hold one value while dozens
            # held several. Observing the document is more reliable than asking
            # the model: if this document states four `client_industries` for
            # one entity, the attribute takes more than one value, and that is
            # a fact about the data rather than a judgement call.
            fact.multivalue
            or (fact.namespace, fact.attribute_name) in document.multivalued,
        )
        fact_entity_id = await resolve(fact.entity_type, fact.entity_name)
        # Unique on (entity, attribute, value), so re-extracting the same fact
        # from another chunk or re-ingesting the document cannot create a
        # duplicate row. DO UPDATE rather than DO NOTHING purely so the id
        # comes back on a conflict too -- provenance has to be recorded for a
        # fact this version restates, not only for one it states first, or a
        # document would appear to have stopped asserting everything it shares
        # with another.
        stmt = (
            pg_insert(Value)
            .values(
                entity_id=fact_entity_id,
                attribute_id=attribute_id,
                value=fact.value,
                value_norm=normalize_value(fact.value),
                searchable=fact.searchable,
            )
            # On the normalized form, so a value that differs only by case or
            # by which Unicode hyphen the model reached for is recognised as
            # the fact it already is.
            .on_conflict_do_update(
                index_elements=[
                    Value.entity_id,
                    Value.attribute_id,
                    Value.value_norm,
                ],
                set_={"searchable": fact.searchable},
            )
            .returning(Value.id)
        )
        value_id = (await session.execute(stmt)).scalar_one()

        await session.execute(
            pg_insert(ValueProvenance)
            .values(value_id=value_id, version_id=version_id)
            .on_conflict_do_nothing(
                index_elements=[ValueProvenance.value_id, ValueProvenance.version_id]
            )
        )

    for relation in extraction.relations:
        source_id = await resolve(relation.source_entity_type, relation.source_entity_name)
        target_id = await resolve(relation.target_entity_type, relation.target_entity_name)
        # Same idempotency guarantee as facts: a relation (source, target,
        # type) is written at most once, so "Alpinist Studios employs Justin
        # Flores" can't appear twice.
        stmt = (
            pg_insert(Relation)
            .values(
                source_entity_id=source_id,
                target_entity_id=target_id,
                relation_type=relation.relation_type,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    Relation.source_entity_id,
                    Relation.target_entity_id,
                    Relation.relation_type,
                ]
            )
        )
        await session.execute(stmt)

    return entity_id


FactKey = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class _DocumentFacts:
    """What a whole document says, once its restatements are folded together.

    Both answers here need the *document*, not a chunk: a document's four
    `client_industries` are usually spread over four chunks, and the two
    tellings of one definition usually land in two overlapping windows.
    """

    surviving: dict[FactKey, frozenset[str]]
    multivalued: frozenset[tuple[str, str]]

    def keeps(self, key: FactKey, value: str) -> bool:
        survivors = self.surviving.get(key)
        return survivors is None or value in survivors


def _analyze_document_facts(
    extractions: tuple[ChunkExtraction, ...],
) -> _DocumentFacts:
    """Fold each entity+attribute's values, then decide which are lists.

    Order matters: `multivalue` is derived from what *survives* collapsing.
    Deriving it first would read two tellings of one definition as evidence
    that `definition` takes several values, which is the opposite of what
    they are evidence of.
    """
    grouped: dict[FactKey, list[str]] = {}
    for extraction in extractions:
        for fact in extraction.facts:
            key = (
                fact.entity_type,
                fact.entity_name,
                fact.namespace,
                fact.attribute_name,
            )
            values = grouped.setdefault(key, [])
            if fact.value not in values:
                values.append(fact.value)

    surviving = {
        key: collapse_near_duplicates(values) for key, values in grouped.items()
    }
    if any(len(v) != len(grouped[k]) for k, v in surviving.items()):
        logger.info(
            "Collapsed %d restated fact(s) across %d attribute(s).",
            sum(len(grouped[k]) - len(v) for k, v in surviving.items()),
            sum(1 for k, v in surviving.items() if len(v) != len(grouped[k])),
        )

    return _DocumentFacts(
        surviving={key: frozenset(values) for key, values in surviving.items()},
        multivalued=frozenset(
            (namespace, attribute_name)
            for (_, _, namespace, attribute_name), values in surviving.items()
            if len(values) > 1
        ),
    )


async def persist_chunk_extractions(
    session: AsyncSession, version_id: UUID, extractions: tuple[ChunkExtraction, ...]
) -> int:
    """Persist every chunk's EAV extraction (steps 5-6). Returns the count
    of distinct entities resolved, for job reporting."""
    document = _analyze_document_facts(extractions)
    resolved_ids = set()
    for extraction in extractions:  # each may write rows depending on prior ones
        entity_id = await _persist_one_extraction(
            session, version_id, extraction, document
        )
        resolved_ids.add(entity_id)
    await session.flush()
    return len(resolved_ids - {None})


async def resolve_superseded_values(session: AsyncSession) -> tuple[int, int]:
    """Recompute which facts are current. Returns (superseded, restored).

    A value is **current** when at least one version that asserts it is its
    source's `current_version_id`, and **superseded** when none of them are.
    Nothing here is incremental: the state is derived from provenance every
    time, so a re-ingest, a rollback, or a document that stops being current
    all converge on the same answer without anyone having to reason about the
    order they happened in.

    Two rules, and the second matters as much as the first:

    * A fact no current document asserts is marked superseded. It is not
      deleted -- the rate that used to apply is a real historical fact, and
      the mark is what stops it being answered as though it still applied.
    * A fact that becomes current again is **un-marked**. Facts come back:
      a value removed in v2 and restored in v3 is current again, and a
      one-way mark would leave it permanently invisible.

    Runs after cutover, not before. `current_version_id` is what "current"
    means here, and the cutover is what sets it -- running this first would
    mark the version being ingested as superseded, since it is not yet the
    current one.

    Values with no provenance at all are never touched. Every row that
    predates provenance tracking is in that state, so this is safe to run
    against a database whose history was never recorded: those facts stay
    current, which is what they were before this existed.
    """
    asserted_by_a_current_version = (
        select(ValueProvenance.value_id)
        .join(
            KnowledgeSource,
            KnowledgeSource.current_version_id == ValueProvenance.version_id,
        )
        .where(ValueProvenance.value_id == Value.id)
        .exists()
    )
    has_any_provenance = (
        select(ValueProvenance.value_id)
        .where(ValueProvenance.value_id == Value.id)
        .exists()
    )

    superseded = (
        await session.execute(
            update(Value)
            .where(
                Value.superseded_at.is_(None),
                has_any_provenance,
                ~asserted_by_a_current_version,
            )
            .values(superseded_at=datetime.now(UTC).replace(tzinfo=None))
        )
    ).rowcount

    restored = (
        await session.execute(
            update(Value)
            .where(Value.superseded_at.is_not(None), asserted_by_a_current_version)
            .values(superseded_at=None)
        )
    ).rowcount

    await session.flush()
    if superseded or restored:
        logger.info(
            "fact currency: %d superseded, %d restored to current",
            superseded,
            restored,
        )
    return superseded, restored

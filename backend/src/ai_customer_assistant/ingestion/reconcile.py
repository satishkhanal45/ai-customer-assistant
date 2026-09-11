"""One-time reconciliation of pre-ontology EAV data.

The ingestion write path (``ingestion/persistence._resolve_entity``) has
canonicalized ``entity.entity_type`` / ``attribute.namespace`` since the
"Ontology of ingestion" change, so new writes collapse synonyms
(``company``/``organization``/``Company`` -> ``Company``) into one row. But
databases ingested *before* that change still carry raw LLM-emitted type
labels, which produces duplicate rows for the same real-world entity (e.g.
"Alpinist Studios" stored once per type variant).

``reconcile`` fixes that stale data in a single transaction:

  1. **Entities**: canonicalize ``entity_type``, merge every group of rows
     that share a (canonical type, case-insensitive name) onto one survivor,
     repointing ``value`` / ``relation`` (both sides) / ``embedding_chunk`` /
     ``knowledge_source_entity_map`` foreign keys, then deleting the
     duplicates. Single-row entities simply get their type normalized.
  2. **Relations**: collapse any ``(source, target, type)`` duplicates left
     by the repointing.
  3. **Attributes**: canonicalize ``attribute.namespace`` the same way and
     merge duplicate ``(namespace, name)`` rows, repointing ``value.attribute_id``.
  4. **Values**: collapse any ``(entity, attribute, value)`` duplicates.

The two pure planners (``build_entity_merge_groups`` /
``build_attribute_merge_groups``) are separated from the DB I/O so the
merge logic is unit-testable without a Postgres connection. ``reconcile``
itself is idempotent: a second run finds no duplicate groups and is a no-op.

Run against a live database with ``uv run python -m ingestion.reconcile``
(reads ``POSTGRES_*`` env vars via ``db.session``, exactly like the server).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import Text, delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Attribute, EmbeddingChunk, Entity, KnowledgeSourceEntityMap, Relation, Value
from ingestion.extraction.ontology import safe_canonicalize_entity_type

EntityRow = tuple[UUID, str, str]       # (id, entity_type, name)
AttributeRow = tuple[UUID, str, str]    # (id, namespace, name)


@dataclass(frozen=True)
class MergeGroup:
    """A set of rows that represent the same real-world thing and should
    collapse onto a single ``survivor_id``."""

    # (chosen type / namespace, normalized name). `key[0]` is written onto
    # the survivor row, so for entities it is the type the merge decided on
    # rather than simply the canonical form of one member's type.
    key: tuple[str, str]
    survivor_id: UUID
    duplicate_ids: tuple[UUID, ...]
    # The display label to write onto the survivor. Entities only -- chosen
    # independently of which row survives, so a merge cannot replace "PyTorch"
    # with "pytorch" just because the lowercase row happened to hold more
    # facts. None leaves the name untouched.
    survivor_name: str | None = None


@dataclass
class ReconcileReport:
    entities_merged: int = 0
    entities_normalized: int = 0
    attributes_merged: int = 0
    attributes_normalized: int = 0
    relations_deduped: int = 0
    values_deduped: int = 0

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"merged {self.entities_merged} duplicate entities "
            f"(normalized {self.entities_normalized} entity types), "
            f"merged {self.attributes_merged} duplicate attributes "
            f"(normalized {self.attributes_normalized} namespaces), "
            f"deduped {self.relations_deduped} relations and "
            f"{self.values_deduped} values"
        )


def _normalized_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


# A type must carry at least this share of a group's facts to be considered
# for the merged row's label.
#
# Without it, rarity alone picks a type that is rare *because the model
# invented it once*: "Development Process" holds 2 of Agile's 55 facts and
# appears on 2 entities in the whole corpus, so pure rarity would have made it
# the label over "Methodology", which holds 39.
_TYPE_SUPPORT_SHARE = 0.2


def _capitals(text: str) -> int:
    return sum(1 for ch in text if ch.isupper())


def build_entity_merge_groups(
    rows: Iterable[EntityRow],
    *,
    fact_counts: Mapping[UUID, int] | None = None,
    type_frequency: Mapping[str, int] | None = None,
) -> list[MergeGroup]:
    """Plan the entity merge: **group rows by normalized name alone**.

    Grouping used to include the canonical entity type, which meant it only
    ever merged rows whose types were synonyms of one canonical term. That
    left the larger problem untouched: "Agile" stored as ``Methodology``,
    ``Process`` and ``Development Process`` are not synonyms, so they stayed
    three entities holding three disjoint sets of facts about one concept.

    Three choices, deliberately made independently. Letting one row win all
    three is what produced the bad outcomes when this was first measured.

    * **Which row survives** -- the one with the most facts, so the merge
      repoints as few rows as possible. Nothing user-visible depends on it.
    * **The display label** -- the best-cased variant, by capital count. The
      survivor row is often the lowercase one, and taking its name would have
      renamed "PyTorch" to "pytorch" and "eBay" to "ebay".
    * **The type** -- the rarest type in the corpus, as a proxy for
      specificity (``Technology`` spans 80 entities, ``Programming Language``
      2), restricted to types that carry a real share of the group's facts.
      ``REVIEWED_ENTITY_TYPES`` overrides the heuristic where a human has
      already made the call.

    ``fact_counts`` and ``type_frequency`` are injected rather than queried so
    this stays a pure function. Omitting them degrades gracefully to a
    deterministic id-ordered choice, which is what the unit tests exercise.
    """
    facts = fact_counts or {}
    frequency = type_frequency or {}

    buckets: dict[str, list[EntityRow]] = {}
    for row in rows:
        buckets.setdefault(_normalized_name(row[2]), []).append(row)

    groups: list[MergeGroup] = []
    for normalized, members in sorted(buckets.items()):
        if len(members) < 2:
            continue

        canonical = {m[0]: safe_canonicalize_entity_type(m[1]) for m in members}
        top = max(facts.get(m[0], 0) for m in members)
        supported = [
            m for m in members if facts.get(m[0], 0) >= max(1, top * _TYPE_SUPPORT_SHARE)
        ] or members

        most_specific = min(
            supported,
            key=lambda m: (
                frequency.get(canonical[m[0]], 0),   # rarest type in the corpus
                -facts.get(m[0], 0),                 # then best supported here
                canonical[m[0]],                     # then stable by name
                str(m[0]),
            ),
        )
        chosen_type = (
            REVIEWED_ENTITY_TYPES.get(normalized) or canonical[most_specific[0]]
        )

        # Most facts; then a row whose stored type is already the chosen one
        # (its label needs no rewriting, and among pure synonyms -- `company`,
        # `Company`, `organization` -- it is the only thing distinguishing
        # them); then the canonical match; then the lowest id, so the choice
        # never depends on which uuid happened to be generated first.
        survivor = min(
            members,
            key=lambda m: (
                -facts.get(m[0], 0),
                m[1] != chosen_type,
                canonical[m[0]] != chosen_type,
                str(m[0]),
            ),
        )

        label = max(members, key=lambda m: (_capitals(m[2]), len(m[2]), str(m[0])))[2]

        groups.append(
            MergeGroup(
                key=(chosen_type, normalized),
                survivor_id=survivor[0],
                duplicate_ids=tuple(
                    m[0] for m in sorted(members, key=lambda m: str(m[0]))
                    if m[0] != survivor[0]
                ),
                survivor_name=label,
            )
        )
    return groups


def build_attribute_merge_groups(rows: Iterable[AttributeRow]) -> list[MergeGroup]:
    """Plan the attribute merge by (canonical namespace, name). Same survivor
    policy as entities: prefer the exact-canonical namespace, else lowest id."""
    buckets: dict[tuple[str, str], list[AttributeRow]] = {}
    for row in rows:
        aid, namespace, name = row
        key = (safe_canonicalize_entity_type(namespace), name.strip().lower())
        buckets.setdefault(key, []).append(row)

    groups: list[MergeGroup] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        canonical_namespace = key[0]
        exact = [m for m in members if m[1] == canonical_namespace]
        ordered = sorted(members, key=lambda m: m[0])
        survivor = exact[0] if exact else ordered[0]
        groups.append(
            MergeGroup(
                key=key,
                survivor_id=survivor[0],
                duplicate_ids=tuple(m[0] for m in ordered if m[0] != survivor[0]),
            )
        )
    return groups


async def _repoint_entity_merge(
    session: AsyncSession, group: MergeGroup, report: ReconcileReport
) -> None:
    """Fold every row in ``group.duplicate_ids`` into the survivor, avoiding
    unique-constraint violations by first deleting rows that would collide."""
    survivor = group.survivor_id
    dups = list(group.duplicate_ids)

    # Values: drop duplicate-entity rows that already exist on the survivor,
    # then repoint the rest.
    #
    # Matched on `value_norm`, not `value`. Uniqueness moved to the normalized
    # form, so the exact-text comparison would miss a collision and the UPDATE
    # below would fail on the constraint -- and merging is exactly what makes
    # these collide, because "offers flexibility" on `Methodology / Agile` and
    # on `Process / Agile` only meet once the two entities are one.
    survivor_values = select(Value.attribute_id, Value.value_norm).where(
        Value.entity_id == survivor
    )
    await session.execute(
        delete(Value).where(
            Value.entity_id.in_(dups),
            tuple_(Value.attribute_id, Value.value_norm).in_(survivor_values),
        )
    )
    # Two duplicates may also collide with *each other* rather than with the
    # survivor. Keep one row per (attribute, normalized value) across the
    # whole group before repointing.
    keep_per_key = (
        select(func.min(Value.id.cast(Text)))
        .where(Value.entity_id.in_(dups))
        .group_by(Value.attribute_id, Value.value_norm)
    )
    await session.execute(
        delete(Value).where(
            Value.entity_id.in_(dups),
            Value.id.cast(Text).not_in(keep_per_key),
        )
    )
    await session.execute(update(Value).where(Value.entity_id.in_(dups)).values(entity_id=survivor))

    # Relations (outgoing): drop duplicates of an existing survivor relation.
    survivor_out = select(Relation.target_entity_id, Relation.relation_type).where(
        Relation.source_entity_id == survivor
    )
    await session.execute(
        delete(Relation).where(
            Relation.source_entity_id.in_(dups),
            tuple_(Relation.target_entity_id, Relation.relation_type).in_(survivor_out),
        )
    )
    await session.execute(
        update(Relation).where(Relation.source_entity_id.in_(dups)).values(source_entity_id=survivor)
    )

    # Relations (incoming): drop duplicates of an existing survivor relation.
    survivor_in = select(Relation.source_entity_id, Relation.relation_type).where(
        Relation.target_entity_id == survivor
    )
    await session.execute(
        delete(Relation).where(
            Relation.target_entity_id.in_(dups),
            tuple_(Relation.source_entity_id, Relation.relation_type).in_(survivor_in),
        )
    )
    await session.execute(
        update(Relation).where(Relation.target_entity_id.in_(dups)).values(target_entity_id=survivor)
    )

    # Chunks / source maps: no uniqueness on the merged column — just repoint.
    await session.execute(
        update(EmbeddingChunk).where(EmbeddingChunk.entity_id.in_(dups)).values(entity_id=survivor)
    )
    await session.execute(
        update(KnowledgeSourceEntityMap)
        .where(KnowledgeSourceEntityMap.entity_id.in_(dups))
        .values(entity_id=survivor)
    )

    # Delete the duplicates *before* relabelling the survivor.
    #
    # The survivor takes the group's chosen type and name, and those may be
    # exactly what one of the duplicates is still holding -- merging the two
    # "Design" rows relabels the `Phase` survivor to `SDLC Phase`, which the
    # `SDLC Phase` duplicate already occupies. Relabelling first hits
    # `uq_entity_type_name` against a row that is about to cease existing.
    #
    # Safe in this order because every foreign key that pointed at a duplicate
    # -- values, relations both ways, chunks, source maps -- has been repointed
    # to the survivor above.
    await session.execute(delete(Entity).where(Entity.id.in_(dups)))

    survivor_values_to_write: dict[str, str] = {"entity_type": group.key[0]}
    if group.survivor_name is not None:
        # Chosen independently of which row survived, so the merge cannot
        # rename "PyTorch" to "pytorch".
        survivor_values_to_write["name"] = group.survivor_name
        survivor_values_to_write["label"] = group.survivor_name
    await session.execute(
        update(Entity).where(Entity.id == survivor).values(**survivor_values_to_write)
    )
    report.entities_merged += len(dups)


async def _normalize_singleton_entity_types(
    session: AsyncSession, rows: Sequence[EntityRow], report: ReconcileReport
) -> None:
    """Normalize the entity_type of rows that were alone in their merge group
    (their type label still points at a non-canonical string)."""
    for eid, entity_type, name in rows:
        canonical = safe_canonicalize_entity_type(entity_type)
        if canonical != entity_type:
            await session.execute(update(Entity).where(Entity.id == eid).values(entity_type=canonical))
            report.entities_normalized += 1


async def _repoint_attribute_merge(
    session: AsyncSession, group: MergeGroup, report: ReconcileReport
) -> None:
    survivor = group.survivor_id
    dups = list(group.duplicate_ids)

    survivor_values = select(Value.entity_id, Value.value).where(Value.attribute_id == survivor)
    await session.execute(
        delete(Value).where(
            Value.attribute_id.in_(dups),
            tuple_(Value.entity_id, Value.value).in_(survivor_values),
        )
    )
    await session.execute(update(Value).where(Value.attribute_id.in_(dups)).values(attribute_id=survivor))

    await session.execute(update(Attribute).where(Attribute.id == survivor).values(namespace=group.key[0]))
    await session.execute(delete(Attribute).where(Attribute.id.in_(dups)))
    report.attributes_merged += len(dups)


async def _normalize_singleton_attribute_namespaces(
    session: AsyncSession, rows: Sequence[AttributeRow], report: ReconcileReport
) -> None:
    for aid, namespace, name in rows:
        canonical = safe_canonicalize_entity_type(namespace)
        if canonical != namespace:
            await session.execute(update(Attribute).where(Attribute.id == aid).values(namespace=canonical))
            report.attributes_normalized += 1


async def _dedupe_relations(session: AsyncSession, report: ReconcileReport) -> None:
    keep = (
        select(Relation.id)
        .distinct(Relation.source_entity_id, Relation.target_entity_id, Relation.relation_type)
        .order_by(
            Relation.source_entity_id,
            Relation.target_entity_id,
            Relation.relation_type,
            Relation.id,
        )
    ).subquery()
    dups = await session.execute(delete(Relation).where(Relation.id.not_in(select(keep.c.id))))
    report.relations_deduped += dups.rowcount or 0


async def _dedupe_values(session: AsyncSession, report: ReconcileReport) -> None:
    keep = (
        select(Value.id)
        .distinct(Value.entity_id, Value.attribute_id, Value.value)
        .order_by(Value.entity_id, Value.attribute_id, Value.value, Value.id)
    ).subquery()
    dups = await session.execute(delete(Value).where(Value.id.not_in(select(keep.c.id))))
    report.values_deduped += dups.rowcount or 0


# ---------------------------------------------------------------------------
# Reviewed single-referent merges
#
# The type-synonym logic above only collapses rows whose types are synonyms
# of ONE canonical type. A second, rarer class of stale duplicate is a
# proper-noun single referent that the LLM typed under *different* (and
# non-synonym) types — e.g. "Amazon Web Services (AWS)" stored once as
# ``cloud_infrastructure`` and once as ``cloud provider``. Merging those by
# name is only safe when the name unambiguously names ONE thing, so this is
# an explicit, manually-reviewed allowlist — NOT a heuristic. Every entry
# was verified against the extracted relations: the rows refer to the same
# real-world referent and their relations are complementary facts, not
# contradictory ones.
# ---------------------------------------------------------------------------

#: normalized name -> entity type to use for the merged survivor row.
REVIEWED_ENTITY_TYPES: dict[str, str] = {
    "postgresql": "Database",
    "amazon web services (aws)": "Cloud Platform",
    "microsoft azure": "Cloud Platform",
}

#: Backwards-compatible alias. The list used to drive a separate merge pass;
#: merging by name now subsumes that, and what remains useful is the reviewed
#: *type* for names where the frequency heuristic would choose worse --
#: "PostgreSQL" carries 8 facts as ``Technology`` and 1 as ``Database``, so
#: support alone would keep the vaguer label.
PROPER_NOUN_MERGES = REVIEWED_ENTITY_TYPES


async def reconcile(session: AsyncSession) -> ReconcileReport:
    """Canonicalize and merge stale EAV rows. Idempotent and transactional:
    the caller commits (or rolls back) as one unit."""
    report = ReconcileReport()

    entity_rows = (
        (await session.execute(select(Entity.id, Entity.entity_type, Entity.name))).all()
    )
    fact_counts = {
        row.entity_id: row.n
        for row in (
            await session.execute(
                select(Value.entity_id, func.count().label("n")).group_by(Value.entity_id)
            )
        ).all()
    }
    type_frequency = {
        row.entity_type: row.n
        for row in (
            await session.execute(
                select(Entity.entity_type, func.count().label("n")).group_by(
                    Entity.entity_type
                )
            )
        ).all()
    }
    entity_groups = build_entity_merge_groups(
        entity_rows, fact_counts=fact_counts, type_frequency=type_frequency
    )
    for group in entity_groups:
        await _repoint_entity_merge(session, group, report)
    await _normalize_singleton_entity_types(session, entity_rows, report)

    await _dedupe_relations(session, report)

    attribute_rows = (
        (await session.execute(select(Attribute.id, Attribute.namespace, Attribute.name))).all()
    )
    attribute_groups = build_attribute_merge_groups(attribute_rows)
    for group in attribute_groups:
        await _repoint_attribute_merge(session, group, report)
    await _normalize_singleton_attribute_namespaces(session, attribute_rows, report)

    await _dedupe_values(session, report)
    return report


async def _main() -> None:
    from db.async_session import session_factory

    async with session_factory() as session:
        report = await reconcile(session)
        await session.commit()
    print(report)


if __name__ == "__main__":
    asyncio.run(_main())
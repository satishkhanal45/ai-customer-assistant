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
from typing import Iterable, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Attribute, EmbeddingChunk, Entity, KnowledgeSourceEntityMap, Relation, Value
from ingestion.extraction.ontology import safe_canonicalize_entity_type

EntityRow = tuple[UUID, str, str]       # (id, entity_type, name)
AttributeRow = tuple[UUID, str, str]    # (id, namespace, name)


@dataclass(frozen=True)
class MergeGroup:
    """A set of rows that represent the same real-world thing and should
    collapse onto a single ``survivor_id``."""

    key: tuple[str, str]                  # (canonical type, normalized name)
    survivor_id: UUID
    duplicate_ids: tuple[UUID, ...]


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


def build_entity_merge_groups(rows: Iterable[EntityRow]) -> list[MergeGroup]:
    """Plan the entity merge: group rows by (canonical entity type, normalized
    name). A group is only returned when it has >1 member (needs merging).

    Survivor selection: prefer the row whose ``entity_type`` is already the
    canonical one (its attributes are the canonical schema); otherwise the
    oldest (lowest id) row wins. All others are duplicates."""
    buckets: dict[tuple[str, str], list[EntityRow]] = {}
    for row in rows:
        eid, entity_type, name = row
        key = (safe_canonicalize_entity_type(entity_type), _normalized_name(name))
        buckets.setdefault(key, []).append(row)

    groups: list[MergeGroup] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        canonical_type = key[0]
        exact = [m for m in members if m[1] == canonical_type]
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
    # then repoint the rest (unique on entity+attribute+value).
    survivor_values = select(Value.attribute_id, Value.value).where(Value.entity_id == survivor)
    await session.execute(
        delete(Value).where(
            Value.entity_id.in_(dups),
            tuple_(Value.attribute_id, Value.value).in_(survivor_values),
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

    await session.execute(update(Entity).where(Entity.id == survivor).values(entity_type=group.key[0]))
    await session.execute(delete(Entity).where(Entity.id.in_(dups)))
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

#: name -> canonical entity type for the merged survivor row.
PROPER_NOUN_MERGES: dict[str, str] = {
    "PostgreSQL": "Database",
    "Amazon Web Services (AWS)": "Cloud Platform",
    "Microsoft Azure": "Cloud Platform",
}


async def _merge_proper_noun(session: AsyncSession, name: str, survivor_type: str, report: ReconcileReport) -> None:
    """Fold every entity named ``name`` (case-insensitive) into one row typed
    ``survivor_type``, preferring a row that already carries that type."""
    rows = (
        (await session.execute(
            select(Entity.id, Entity.entity_type).where(func.lower(Entity.name) == name.lower())
        )).all()
    )
    if len(rows) < 2:
        if rows:
            eid, entity_type = rows[0]
            if entity_type != survivor_type:
                await session.execute(update(Entity).where(Entity.id == eid).values(entity_type=survivor_type))
                report.entities_normalized += 1
        return

    exact = [r for r in rows if r[1] == survivor_type]
    ordered = sorted(rows, key=lambda r: r[0])
    survivor = exact[0] if exact else ordered[0]
    survivor_id = survivor[0]
    dups = [r[0] for r in ordered if r[0] != survivor_id]

    await _repoint_entity_merge(
        session,
        MergeGroup(key=(survivor_type, name.lower()), survivor_id=survivor_id, duplicate_ids=tuple(dups)),
        report,
    )


async def reconcile_proper_nouns(session: AsyncSession, report: ReconcileReport) -> None:
    """Apply the reviewed single-referent merges. Safe to run repeatedly."""
    for name, survivor_type in PROPER_NOUN_MERGES.items():
        await _merge_proper_noun(session, name, survivor_type, report)


async def reconcile(session: AsyncSession) -> ReconcileReport:
    """Canonicalize and merge stale EAV rows. Idempotent and transactional:
    the caller commits (or rolls back) as one unit."""
    report = ReconcileReport()

    entity_rows = (
        (await session.execute(select(Entity.id, Entity.entity_type, Entity.name))).all()
    )
    entity_groups = build_entity_merge_groups(entity_rows)
    for group in entity_groups:
        await _repoint_entity_merge(session, group, report)
    await _normalize_singleton_entity_types(session, entity_rows, report)

    await reconcile_proper_nouns(session, report)

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
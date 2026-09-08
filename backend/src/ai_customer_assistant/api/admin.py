"""Admin read endpoints — sources, jobs, stats, tickets.

The frontend's Admin page has existed since the first frontend milestone
and has shown "Endpoint not available yet" on all four tabs ever since,
because nothing served them. The data was always there: 24 knowledge
sources, 82 ingestion jobs, 339 entities and 7 tickets sat in the database
with no way to look at them short of `psql`.

## Why not `ingestion/storage/api.py`

`frontend_plan.md` §6.2 -- and the note in `main.py` -- point at that module
as the thing to wire. That turns out to be the wrong target: it is a
*write* path (`POST /admin/knowledge-sources`, a manual upload) whose three
placeholder dependencies still raise `NotImplementedError`, and it
duplicates `POST /ingest/upload`, which has worked for months and is what
the Ingest page actually calls. Wiring it would have produced a second
upload route and still left all four read tabs empty. What was missing is
this file.

## Shape

Every response is an object with a named list and a `total`, never a bare
array. A bare array cannot grow a field later without breaking every
caller, and `total` is what tells the caller whether it is looking at all
of the rows or the first page of them -- `len(rows)` cannot answer that
once a limit is involved. (The frontend accepts either shape; that is
tolerance on its side, not licence to return the weaker one.)

Reads are admin-only. They expose the whole corpus's structure, every
error message an ingestion has ever produced, and the email address of
everyone who has opened a ticket -- so `require_admin`, not
`require_member`.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import Principal, require_admin
from db.engine import get_session

logger = logging.getLogger(__name__)

# Every route here requires an admin. Declared on the router rather than
# per-endpoint so a route added later is protected by default -- the
# failure mode of per-endpoint dependencies is the one somebody forgets,
# and it fails open.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

# A page size, not a safety valve: these tables are small today, and a
# caller that wants everything can page. The cap exists so one request
# cannot ask the database to serialise an unbounded result set.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]


async def _count(session: AsyncSession, model) -> int:
    """Row count for one table, as an int rather than a Row."""
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@router.get("/knowledge-sources")
async def list_sources(
    session: AsyncSession = Depends(get_session),
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> dict:
    """Every knowledge source with the status of its *current* version.

    The status a person cares about is the current version's, not the
    source's -- a source is just a name and a category; whether it is
    searchable is a property of the version that is live. Both joins are
    LEFT: a source registered but not yet ingested has no current version,
    and category is optional.
    """
    from db.models import KnowledgeCategory, KnowledgeSource, KnowledgeSourceVersion

    statement = (
        select(
            KnowledgeSource,
            KnowledgeSourceVersion.status,
            KnowledgeSourceVersion.version_number,
            KnowledgeSourceVersion.file_size_bytes,
            KnowledgeCategory.name,
        )
        .select_from(KnowledgeSource)
        .outerjoin(
            KnowledgeSourceVersion,
            KnowledgeSourceVersion.version_id == KnowledgeSource.current_version_id,
        )
        .outerjoin(
            KnowledgeCategory,
            KnowledgeCategory.category_id == KnowledgeSource.category_id,
        )
        .order_by(KnowledgeSource.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = (await session.execute(statement)).all()
    return {
        "total": await _count(session, KnowledgeSource),
        "limit": limit,
        "offset": offset,
        "sources": [
            {
                "source_id": str(source.source_id),
                "source_name": source.source_name,
                "source_type": source.source_type,
                "category_name": category_name,
                "version_status": status,
                "version_number": version_number,
                "file_size_bytes": size,
                "is_active": source.is_active,
                "created_at": source.created_at.isoformat() if source.created_at else None,
                "updated_at": source.updated_at.isoformat() if source.updated_at else None,
            }
            for source, status, version_number, size, category_name in rows
        ],
    }


@router.get("/jobs")
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    status: str | None = None,
) -> dict:
    """Ingestion jobs -- work in progress first, then most recent activity.

    Ordering this well matters more than it sounds. The obvious version,
    `started_at DESC NULLS FIRST`, was wrong on the real table: 37 of 82
    rows predate the code that stamps `started_at`, so NULLS FIRST filled
    the entire first page with the oldest failures in the database and
    buried everything that had run since.

    So: anything QUEUED or RUNNING first -- a queue you cannot see the head
    of is not much use -- then by whichever of `completed_at` or
    `started_at` the row actually has, newest first, with rows that have
    neither sorted last rather than first.
    """
    from db.models import KnowledgeInjectionJob, KnowledgeSource

    pending_first = case(
        (KnowledgeInjectionJob.status.in_(("QUEUED", "RUNNING")), 0), else_=1
    )
    recency = func.coalesce(
        KnowledgeInjectionJob.completed_at, KnowledgeInjectionJob.started_at
    )

    statement = (
        select(KnowledgeInjectionJob, KnowledgeSource.source_name)
        .select_from(KnowledgeInjectionJob)
        .outerjoin(
            KnowledgeSource,
            KnowledgeSource.source_id == KnowledgeInjectionJob.source_id,
        )
        .order_by(pending_first, recency.desc().nullslast())
        .limit(limit)
        .offset(offset)
    )
    if status:
        statement = statement.where(KnowledgeInjectionJob.status == status.upper())

    rows = (await session.execute(statement)).all()

    # A breakdown by status, so the page can say "3 failed" without pulling
    # every row and counting them client-side.
    by_status = {
        row.status: row.n
        for row in (
            await session.execute(
                select(
                    KnowledgeInjectionJob.status.label("status"),
                    func.count().label("n"),
                ).group_by(KnowledgeInjectionJob.status)
            )
        ).all()
    }

    return {
        "total": await _count(session, KnowledgeInjectionJob),
        "limit": limit,
        "offset": offset,
        "by_status": by_status,
        "jobs": [
            {
                "job_id": str(job.job_id),
                "source_id": str(job.source_id),
                "source_name": source_name,
                "job_type": job.job_type,
                "status": job.status,
                "chunks_created_count": job.chunks_created_count,
                "entities_created_count": job.entities_created_count,
                "triggered_by": job.triggered_by,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                "error_details": job.error_details,
            }
            for job, source_name in rows
        ],
    }


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    """Corpus counts, and the entity type distribution.

    This is also what the Overview page's figures come from. Before this
    endpoint existed, two of its four cards showed a dash and the third
    counted the rows one capped `/graph/search` happened to return -- which
    reported the query limit as if it were the total.
    """
    from db.models import (
        EmbeddingChunk,
        Entity,
        KnowledgeInjectionJob,
        KnowledgeSource,
        Relation,
        Ticket,
    )

    by_type = [
        {"entity_type": row.entity_type, "count": row.n}
        for row in (
            await session.execute(
                select(Entity.entity_type.label("entity_type"), func.count().label("n"))
                .group_by(Entity.entity_type)
                .order_by(func.count().desc())
            )
        ).all()
    ]

    return {
        "entities": {"total": await _count(session, Entity)},
        "relations": {"total": await _count(session, Relation)},
        "sources": {"total": await _count(session, KnowledgeSource)},
        "chunks": {"total": await _count(session, EmbeddingChunk)},
        "jobs": {"total": await _count(session, KnowledgeInjectionJob)},
        "tickets": {"total": await _count(session, Ticket)},
        "by_type": by_type,
    }


@router.get("/tickets")
async def list_tickets(
    session: AsyncSession = Depends(get_session),
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    status: str | None = None,
) -> dict:
    """Support tickets, newest first.

    Every row carries a customer's email address, which is why this router
    is admin-only rather than member-only.
    """
    from db.models import Ticket

    statement = (
        select(Ticket).order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
    )
    if status:
        statement = statement.where(Ticket.status == status.upper())

    tickets = (await session.execute(statement)).scalars().all()

    by_status = {
        row.status: row.n
        for row in (
            await session.execute(
                select(Ticket.status.label("status"), func.count().label("n")).group_by(
                    Ticket.status
                )
            )
        ).all()
    }

    return {
        "total": await _count(session, Ticket),
        "limit": limit,
        "offset": offset,
        "by_status": by_status,
        "tickets": [
            {
                "ticket_id": str(ticket.ticket_id),
                "email": ticket.email,
                "query": ticket.query,
                "reason": ticket.reason,
                "priority": ticket.priority,
                "status": ticket.status,
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
            }
            for ticket in tickets
        ],
    }

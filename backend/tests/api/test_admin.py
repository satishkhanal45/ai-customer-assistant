"""The admin read endpoints (`api/admin.py`).

These four routes are what the frontend's Admin page has been asking for
since it was written; until now every tab rendered "Endpoint not available
yet". The tests below cover the three things worth pinning:

* **who may read them** -- these expose the whole corpus's structure, every
  ingestion error message, and the email address of everyone who has opened
  a ticket, so `member` is not enough;
* **the response shape** -- an object with a named list and a `total`,
  because a bare array cannot say whether you are looking at all of the
  rows or the first page of them;
* **the ordering of jobs**, which had a real bug: `started_at DESC NULLS
  FIRST` put every row that predates the column's introduction at the top,
  filling the first page with the oldest failures in the database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from auth import roles
from db.models import (
    AppUser,
    EmbeddingChunk,
    Entity,
    KnowledgeCategory,
    KnowledgeInjectionJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
    Relation,
    Ticket,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def admin_db():
    """A database holding just the tables these endpoints read."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        AppUser.__table__,
        KnowledgeCategory.__table__,
        KnowledgeSource.__table__,
        KnowledgeSourceVersion.__table__,
        KnowledgeInjectionJob.__table__,
        Entity.__table__,
        Relation.__table__,
        Ticket.__table__,
        # `/admin/stats` counts chunks, so the table has to exist even
        # though nothing here inserts one.
        EmbeddingChunk.__table__,
    ]
    async with engine.begin() as conn:
        for table in tables:
            await conn.run_sync(table.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(admin_db):
    """One category, two sources, three jobs, two tickets, two entities.

    The jobs are deliberately awkward: one still queued with no timestamps
    at all, one old row with no `started_at` (the shape that broke the
    original ordering), and one that ran recently.
    """
    source_a, source_b = uuid.uuid4(), uuid.uuid4()
    version_a = uuid.uuid4()
    category = uuid.uuid4()

    async with admin_db() as session:
        session.add(KnowledgeCategory(category_id=category, name="Handbook"))
        session.add(
            KnowledgeSourceVersion(
                version_id=version_a, source_id=source_a, version_number=3,
                storage_uri="s3://x/a", checksum="check-a", status="INDEXED",
                file_size_bytes=2048, created_at=NOW,
            )
        )
        session.add(
            KnowledgeSource(
                source_id=source_a, source_name="handbook.pdf", source_type="FILE_UPLOAD",
                category_id=category, current_version_id=version_a, is_active=True,
                created_at=NOW, updated_at=NOW,
            )
        )
        # No current version and no category: the LEFT JOIN case.
        session.add(
            KnowledgeSource(
                source_id=source_b, source_name="pending.pdf", source_type="FILE_UPLOAD",
                is_active=False, created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
            )
        )

        session.add(KnowledgeInjectionJob(
            job_id=uuid.uuid4(), source_id=source_a, job_type="INITIAL_INGEST",
            status="QUEUED", triggered_by="test"))
        session.add(KnowledgeInjectionJob(
            job_id=uuid.uuid4(), source_id=source_a, job_type="INITIAL_INGEST",
            status="FAILED", triggered_by="test", error_details="tika exploded",
            completed_at=NOW - timedelta(days=30)))          # legacy: no started_at
        session.add(KnowledgeInjectionJob(
            job_id=uuid.uuid4(), source_id=source_a, job_type="REINDEX",
            status="SUCCEEDED", triggered_by="test", chunks_created_count=7,
            entities_created_count=2, started_at=NOW - timedelta(minutes=5),
            completed_at=NOW))

        session.add(Ticket(ticket_id=uuid.uuid4(), email="a@example.com",
                           query="refund please", status="OPEN", created_at=NOW))
        session.add(Ticket(ticket_id=uuid.uuid4(), email="b@example.com",
                           query="older", status="RESOLVED",
                           created_at=NOW - timedelta(days=2)))

        # Entity's primary key is `id`, not `entity_id`.
        session.add(Entity(id=uuid.uuid4(), entity_type="Company", name="alpinist", label="Alpinist"))
        session.add(Entity(id=uuid.uuid4(), entity_type="Company", name="soani", label="Soani"))
        session.add(Entity(id=uuid.uuid4(), entity_type="Person", name="sam", label="Sam"))
        await session.commit()

    return {"source_a": source_a}


@pytest_asyncio.fixture
async def client(build_app, admin_db, as_role, seeded):
    from api.admin import router

    app = build_app(router)

    # This app's session must come from the seeded database, not the
    # suite-wide one build_app wires by default.
    from db.engine import get_session

    async def _session():
        async with admin_db() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    as_role(app, role=roles.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestAccess:
    async def test_a_member_is_refused(self, build_app, admin_db, as_role):
        """403, not 404: these rows carry customer email addresses and every
        error the pipeline has ever produced."""
        from api.admin import router
        from db.engine import get_session

        app = build_app(router)

        async def _session():
            async with admin_db() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        as_role(app, role=roles.MEMBER)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for path in ("/admin/knowledge-sources", "/admin/jobs", "/admin/stats", "/admin/tickets"):
                assert (await c.get(path)).status_code == 403, path


class TestSources:
    async def test_lists_sources_with_their_current_version(self, client):
        body = (await client.get("/admin/knowledge-sources")).json()

        assert body["total"] == 2
        handbook = next(s for s in body["sources"] if s["source_name"] == "handbook.pdf")
        assert handbook["version_status"] == "INDEXED"
        assert handbook["version_number"] == 3
        assert handbook["category_name"] == "Handbook"

    async def test_a_source_with_no_version_or_category_still_appears(self, client):
        """Both joins are LEFT. A source registered but never ingested is
        exactly the row an administrator is looking for."""
        body = (await client.get("/admin/knowledge-sources")).json()

        pending = next(s for s in body["sources"] if s["source_name"] == "pending.pdf")
        assert pending["version_status"] is None
        assert pending["category_name"] is None
        assert pending["is_active"] is False

    async def test_newest_first(self, client):
        body = (await client.get("/admin/knowledge-sources")).json()
        assert [s["source_name"] for s in body["sources"]] == ["handbook.pdf", "pending.pdf"]


class TestJobs:
    async def test_queued_work_sorts_above_finished_work(self, client):
        """A queue you cannot see the head of is not much use."""
        body = (await client.get("/admin/jobs")).json()
        assert body["jobs"][0]["status"] == "QUEUED"

    async def test_a_row_with_no_started_at_does_not_take_the_top(self, client):
        """The bug this ordering was rewritten for. `started_at DESC NULLS
        FIRST` put every legacy row first, so the page opened on the oldest
        failures in the database instead of on recent activity."""
        statuses = [j["status"] for j in (await client.get("/admin/jobs")).json()["jobs"]]
        assert statuses == ["QUEUED", "SUCCEEDED", "FAILED"]

    async def test_carries_the_source_name(self, client):
        """Which document failed is the first thing asked of this table."""
        body = (await client.get("/admin/jobs")).json()
        assert all(j["source_name"] == "handbook.pdf" for j in body["jobs"])

    async def test_counts_by_status_without_paging(self, client):
        """So the page can say "1 failed" without pulling every row."""
        body = (await client.get("/admin/jobs?limit=1")).json()
        assert len(body["jobs"]) == 1
        assert body["total"] == 3
        assert body["by_status"] == {"QUEUED": 1, "FAILED": 1, "SUCCEEDED": 1}

    async def test_status_filter(self, client):
        body = (await client.get("/admin/jobs?status=failed")).json()
        assert [j["status"] for j in body["jobs"]] == ["FAILED"]
        assert body["jobs"][0]["error_details"] == "tika exploded"


class TestStats:
    async def test_counts_every_table_the_overview_shows(self, client):
        body = (await client.get("/admin/stats")).json()

        assert body["sources"]["total"] == 2
        assert body["entities"]["total"] == 3
        assert body["jobs"]["total"] == 3
        assert body["tickets"]["total"] == 2

    async def test_entity_types_are_ranked(self, client):
        body = (await client.get("/admin/stats")).json()
        assert body["by_type"][0] == {"entity_type": "Company", "count": 2}


class TestTickets:
    async def test_newest_first_with_status_counts(self, client):
        body = (await client.get("/admin/tickets")).json()

        assert body["total"] == 2
        assert body["tickets"][0]["email"] == "a@example.com"
        assert body["by_status"] == {"OPEN": 1, "RESOLVED": 1}


class TestPaging:
    async def test_total_is_the_table_count_not_the_page_size(self, client):
        """The whole reason these return an object rather than a bare array."""
        body = (await client.get("/admin/knowledge-sources?limit=1")).json()
        assert len(body["sources"]) == 1
        assert body["total"] == 2

    async def test_offset_walks_the_list(self, client):
        page = (await client.get("/admin/knowledge-sources?limit=1&offset=1")).json()
        assert [s["source_name"] for s in page["sources"]] == ["pending.pdf"]

    async def test_an_oversized_limit_is_refused(self, client):
        """A cap, so one request cannot ask the database to serialise an
        unbounded result set."""
        assert (await client.get("/admin/jobs?limit=501")).status_code == 422

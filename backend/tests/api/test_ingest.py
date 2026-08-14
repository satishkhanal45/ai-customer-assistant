"""Minimal coverage for the ingest API (api/ingest.py).

The FastAPI app is built with only the ingest router included and its
``get_session`` dependency overridden by an in-memory fake session, so no
live Postgres is touched.

``api.ingest`` builds a (lazy) async engine at import, so it needs
``POSTGRES_*`` set. Those are set inside a fixture via ``monkeypatch`` and
the module is imported lazily, so the env never leaks to the rest of the
suite (in particular it must not flip ``db.checkpointer`` into the real
Postgres path used by the chat_service tests).

Coverage gap closed: GET /ingest/jobs/{id} had zero test coverage. This adds
the 200/404 paths plus one PAGE-crawl round trip proving GET reflects the
current DB row (not a stale value).
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def ingest(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "test")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    monkeypatch.setenv("POSTGRES_DB", "test")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    from api import ingest as _ingest

    return _ingest


class _FakeJob:
    def __init__(self, job_id, status, chunks=0, entities=0, error=None):
        self.job_id = job_id
        self.status = status
        self.chunks_created_count = chunks
        self.entities_created_count = entities
        self.error_details = error


class _FakeSession:
    def __init__(self, jobs):
        self._jobs = {str(j.job_id): j for j in jobs}

    async def get(self, model, job_id):
        return self._jobs.get(str(job_id))


def _build_app(ingest, fake_session) -> FastAPI:
    app = FastAPI()
    app.include_router(ingest.router)

    async def _override_session():
        yield fake_session

    app.dependency_overrides[ingest.get_session] = _override_session
    return app


@pytest.mark.asyncio
async def test_job_status_existing_job_returns_200(ingest):
    job = _FakeJob(uuid.uuid4(), "COMPLETED", chunks=7, entities=3, error=None)
    app = _build_app(ingest, _FakeSession([job]))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/ingest/jobs/{job.job_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == str(job.job_id)
    assert body["status"] == "COMPLETED"
    assert body["chunks_created_count"] == 7
    assert body["entities_created_count"] == 3
    assert body["error_details"] is None


@pytest.mark.asyncio
async def test_job_status_unknown_job_returns_404(ingest):
    app = _build_app(ingest, _FakeSession([]))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/ingest/jobs/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Job not found"


@pytest.mark.asyncio
async def test_crawl_rejects_selector_without_wait_selector(ingest):
    app = _build_app(ingest, _FakeSession([]))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/ingest/crawl",
            json={"url": "https://example.com/x", "scope": "SITE", "wait_strategy": "selector"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_crawl_page_roundtrip_job_status_transitions(ingest, monkeypatch):
    job_id = uuid.uuid4()
    job = _FakeJob(job_id, "QUEUED", chunks=0, entities=0, error=None)
    app = _build_app(ingest, _FakeSession([job]))

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"<html><body><h1>Title</h1><p>Hello.</p></body></html>"
        url = "http://example.com/page"

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _FakeResponse()

    async def _fake_register(session, **kwargs):
        return SimpleNamespace(job_id=job_id, source_id=uuid.uuid4(), version_id=uuid.uuid4())

    async def _fake_run_job(job_id_):
        job.status = "COMPLETED"
        job.chunks_created_count = 2
        job.entities_created_count = 1

    monkeypatch.setattr(ingest.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(ingest, "register_document_version", _fake_register)
    monkeypatch.setattr(ingest, "_run_job", _fake_run_job)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        submit = await client.post(
            "/ingest/crawl",
            json={"url": "http://example.com/page", "scope": "PAGE"},
        )
        assert submit.status_code == 200
        assert submit.json()["status"] == "submitted"
        assert submit.json()["job_id"] == str(job_id)

        await asyncio.sleep(0)

        status = await client.get(f"/ingest/jobs/{job_id}")
        assert status.status_code == 200
        body = status.json()
        assert body["status"] == "COMPLETED"
        assert body["chunks_created_count"] == 2
        assert body["entities_created_count"] == 1

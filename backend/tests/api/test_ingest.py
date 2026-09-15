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


def _build_app(ingest, fake_session, principal=None) -> FastAPI:
    """The ingest router, with a fake session and a signed-in caller.

    Every route on this router requires an authenticated `member` since
    P0-3. These tests are about ingestion rather than about authentication,
    so the caller is supplied by overriding `get_current_user` -- the single
    dependency that `require_member` and `enforce_ingest_quota` both resolve
    through. `tests/api/test_route_protection.py` is what proves the routes
    are actually protected; repeating that here would only make these tests
    fail for the wrong reason.
    """
    from auth.dependencies import Principal, get_current_user

    app = FastAPI()
    app.include_router(ingest.router)

    async def _override_session():
        yield fake_session

    caller = principal or Principal(
        id=uuid.uuid4(), email="tester@example.com", role="member"
    )
    app.dependency_overrides[ingest.get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: caller
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
async def test_crawl_page_enqueues_only_and_does_not_run_the_job(ingest, monkeypatch):
    """The endpoint registers the document and returns 202 with a job_id.

    It must NOT run the pipeline: that belongs to the worker process. This
    used to `asyncio.create_task(_run_job(...))` inside the request, which
    ran the embedding model in the web worker and orphaned RUNNING rows on
    restart. The job therefore stays QUEUED until a worker claims it."""
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

    async def _fake_safe_get(url, **kwargs):
        return _FakeResponse()

    async def _fake_register(session, **kwargs):
        return SimpleNamespace(job_id=job_id, source_id=uuid.uuid4(), version_id=uuid.uuid4())

    # Patched at `safe_get` rather than at `httpx.AsyncClient`: since P0-3
    # the endpoint fetches through the SSRF guard, which validates the URL,
    # walks redirects one hop at a time and re-checks the address actually
    # connected to. Stubbing httpx underneath all of that would also make
    # this test depend on live DNS for example.com. The guard has its own
    # tests in tests/auth/test_ssrf.py; the case below covers the one thing
    # this endpoint is responsible for -- turning a refusal into a 400.
    monkeypatch.setattr(ingest, "safe_get", _fake_safe_get)
    monkeypatch.setattr(ingest, "register_document_version", _fake_register)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        submit = await client.post(
            "/ingest/crawl",
            json={"url": "http://example.com/page", "scope": "PAGE"},
        )
        assert submit.status_code == 202
        assert submit.json()["status"] == "submitted"
        assert submit.json()["job_id"] == str(job_id)

        # Give any stray background task a chance to run, then prove none did.
        await asyncio.sleep(0)

        status = await client.get(f"/ingest/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_api_module_exposes_no_inline_job_runner(ingest):
    """Guard against the fire-and-forget path being reintroduced."""
    assert not hasattr(ingest, "_run_job")


@pytest.mark.asyncio
async def test_crawl_page_refuses_an_internal_address(ingest):
    """The SSRF hole this endpoint used to be.

    `POST /ingest/crawl` makes the server fetch a URL the caller chose, so
    before P0-3 an authenticated-or-not caller could read the cloud instance
    metadata endpoint -- credentials, in one request -- along with anything
    else bound inside the network. The guard answers 400 rather than 500:
    this is a refused request, not a broken one.
    """
    app = _build_app(ingest, _FakeSession([]))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/ingest/crawl",
            json={"url": "http://169.254.169.254/latest/meta-data/", "scope": "PAGE"},
        )

    assert resp.status_code == 400
    assert "link-local" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_site_discovery_refuses_an_internal_address(ingest):
    """The same guard on the discovery path, which reaches the crawler
    rather than httpx and would otherwise start Chromium first."""
    app = _build_app(ingest, _FakeSession([]))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/ingest/crawl/discover", json={"root_url": "http://127.0.0.1:9200/"}
        )

    assert resp.status_code == 400
    assert "loopback" in resp.json()["detail"]

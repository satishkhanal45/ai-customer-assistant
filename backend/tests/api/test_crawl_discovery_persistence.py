"""Crawl discovery must survive leaving the process that created it (P2-5).

`POST /ingest/crawl/discover` and `POST /ingest/crawl/{id}/confirm` are two
separate HTTP requests with a human review between them. The discovery list
was cached in a module-level dict, so with more than one API instance the
confirm had roughly a coin-flip chance of landing somewhere that had never
heard of the id — and the 404 it returned said "unknown or expired
discovery_id", blaming a TTL that had not elapsed. One instance restarting
between the two calls produced the same misleading error.

The round-trip tests below matter as much as the persistence: `confirm` must
crawl with the settings discovery actually ran under, so both the
`DiscoveryResult` and the `CrawlConfig` have to survive JSON intact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import ingest
from db.models import CrawlDiscovery
from ingestion.crawler.config import CrawlConfig, CrawlMode
from ingestion.crawler.models import DiscoveredPage, DiscoveryResult


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(CrawlDiscovery.__table__.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def result() -> DiscoveryResult:
    return DiscoveryResult(
        pages=(
            DiscoveredPage(url="https://example.com/a", kind="PAGE"),
            DiscoveredPage(url="https://example.com/b.pdf", kind="DOCUMENT", file_type="PDF"),
        ),
        source="SITEMAP",
    )


@pytest.fixture
def config() -> CrawlConfig:
    return CrawlConfig(
        mode=CrawlMode.SITE,
        allowed_domains=("example.com",),
        wait_strategy="selector",
        wait_selector="#main",
        max_pages=17,
    )


class TestSerialisationRoundTrip:
    def test_discovery_survives_json(self, result):
        restored = ingest._deserialize_discovery(ingest._serialize_discovery(result))
        assert restored == result

    def test_config_survives_json(self, config):
        """Notably `mode` (a str-Enum) and `allowed_domains` (a tuple), which
        JSON has no native form for."""
        restored = ingest._deserialize_config(ingest._serialize_config(config))
        assert restored == config
        assert restored.mode is CrawlMode.SITE
        assert isinstance(restored.allowed_domains, tuple)

    def test_the_crawl_settings_are_preserved_exactly(self, config):
        """`confirm` must crawl under the settings discovery ran with. If a
        field is lost here it silently reverts to a default — widening a
        scoped crawl, or changing how pages are waited for."""
        restored = ingest._deserialize_config(ingest._serialize_config(config))
        assert restored.max_pages == 17
        assert restored.wait_strategy == "selector"
        assert restored.wait_selector == "#main"

    def test_an_unknown_field_from_an_older_row_is_ignored(self, config):
        data = ingest._serialize_config(config)
        data["a_field_that_no_longer_exists"] = 1
        assert ingest._deserialize_config(data).max_pages == 17

    def test_a_missing_field_falls_back_to_its_default(self, config):
        data = ingest._serialize_config(config)
        del data["retry_count"]
        assert ingest._deserialize_config(data).retry_count == CrawlConfig().retry_count


class TestPersistenceAcrossProcesses:
    async def test_a_discovery_is_readable_from_another_session(
        self, session_factory, result, config
    ):
        """The whole point: whoever confirms need not be whoever discovered."""
        async with session_factory() as writer:
            discovery_id = await ingest._cache_discovery(writer, result, config)
            await writer.commit()

        async with session_factory() as reader:
            cached = await ingest._get_discovery(reader, discovery_id)

        assert cached is not None
        assert cached.result == result
        assert cached.config == config

    async def test_an_unknown_id_is_a_miss_not_an_error(self, session_factory):
        import uuid

        async with session_factory() as session:
            assert await ingest._get_discovery(session, uuid.uuid4().hex) is None

    async def test_a_malformed_id_is_a_miss_not_a_500(self, session_factory):
        """The id reaches this function straight from the URL path."""
        async with session_factory() as session:
            assert await ingest._get_discovery(session, "not-a-uuid") is None
            assert await ingest._get_discovery(session, "") is None

    async def test_an_expired_discovery_is_gone(self, session_factory, result, config):
        import uuid

        from sqlalchemy import func, select

        expired_id = uuid.uuid4()
        async with session_factory() as session:
            session.add(
                CrawlDiscovery(
                    discovery_id=expired_id,
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    result=ingest._serialize_discovery(result),
                    config=ingest._serialize_config(config),
                )
            )
            await session.commit()

        async with session_factory() as session:
            assert await ingest._get_discovery(session, expired_id.hex) is None
            await session.commit()

        async with session_factory() as session:
            remaining = await session.scalar(select(func.count()).select_from(CrawlDiscovery))
        assert remaining == 0, "expired rows must be swept, not merely hidden"

    async def test_sweeping_does_not_take_live_rows_with_it(
        self, session_factory, result, config
    ):
        import uuid

        async with session_factory() as session:
            live_id = await ingest._cache_discovery(session, result, config)
            session.add(
                CrawlDiscovery(
                    discovery_id=uuid.uuid4(),
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    result=ingest._serialize_discovery(result),
                    config=ingest._serialize_config(config),
                )
            )
            await session.commit()

        async with session_factory() as session:
            assert await ingest._get_discovery(session, live_id) is not None

    async def test_two_discoveries_get_distinct_ids(self, session_factory, result, config):
        async with session_factory() as session:
            first = await ingest._cache_discovery(session, result, config)
            second = await ingest._cache_discovery(session, result, config)
            await session.commit()
        assert first != second


def test_no_module_level_discovery_cache_remains():
    """A grep-style guard. Reintroducing the dict would restore the bug
    without breaking anything else, so nothing else would catch it."""
    assert not hasattr(ingest, "_discovery_cache")

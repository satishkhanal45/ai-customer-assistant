"""Crawler-output worker: runs a crawl, then feeds each successfully
crawled page through the same storage/persistence path as manual
uploads, via ``uploader.handle_crawled_document``.

The crawler package itself (``ingestion.crawler``) needs no modification —
this module only consumes its output.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.models import CrawlDocument
from . import uploader
from .client import SupportsObjectStorage
from .config import StorageConfig


async def ingest_crawl_output(
    documents: Iterable[CrawlDocument],
    *,
    session: AsyncSession,
    client: SupportsObjectStorage,
    config: StorageConfig,
) -> list[uploader.UploadOutcome]:
    """Persist every successfully crawled document, skipping any with a
    crawl-time error. Returns one outcome per document actually attempted,
    in the same relative order — no in-place mutation of ``documents``."""
    successful = (doc for doc in documents if doc.error is None)
    outcomes = [
        await uploader.handle_crawled_document(
            session=session, client=client, config=config, doc=doc
        )
        for doc in successful
    ]
    return outcomes


async def run_crawl_and_ingest(
    crawl: Iterable[CrawlDocument],
    *,
    session: AsyncSession,
    client: SupportsObjectStorage,
    config: StorageConfig,
) -> list[uploader.UploadOutcome]:
    """Convenience wrapper for a scheduler entry point:

        documents = Crawler(crawl_config).crawl(url)
        outcomes = await run_crawl_and_ingest(
            documents, session=session, client=client, config=config
        )
    """
    return await ingest_crawl_output(crawl, session=session, client=client, config=config)

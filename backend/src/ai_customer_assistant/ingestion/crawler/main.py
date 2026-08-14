import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import CrawlConfig, CrawlMode
from .crawler import crawl_confirmed, discover
from .io_output import save_all


def _config_for(url: str) -> CrawlConfig:
    domain = urlsplit(url).netloc
    return CrawlConfig(mode=CrawlMode.SITE, allowed_domains=(domain,))


def _counts(pages) -> tuple[int, int]:
    page_count = sum(1 for p in pages if p.kind == "PAGE")
    doc_count = sum(1 for p in pages if p.kind == "DOCUMENT")
    return page_count, doc_count


async def main():
    args = tuple(sys.argv[1:])
    confirm = "--confirm" in args
    urls = tuple(a for a in args if not a.startswith("--"))

    if not urls:
        print("usage: python -m ingestion.crawler <url> [--confirm]")
        return

    config = _config_for(urls[0])
    result = await discover(urls[0], config)

    page_count, doc_count = _counts(result.pages)
    print("=" * 80)
    print(f"DISCOVERY  ({result.source})")
    print(f"  Pages    : {page_count}")
    print(f"  Documents: {doc_count}")
    print("-" * 80)
    for page in result.pages:
        kind = f"[{page.file_type or 'PAGE'}]" if page.kind == "DOCUMENT" else "[PAGE]"
        print(f"  {kind:10} {page.url}")
    print("=" * 80)

    if not confirm:
        answer = input("Start crawl over these pages? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    docs = await crawl_confirmed(result.pages, config)
    saved = save_all(docs, Path("./output"))

    print("\n" + "=" * 80)
    print("CRAWL RESULTS")
    print("=" * 80)

    for doc, path in zip(docs, saved):
        if doc.error:
            print(f"\n❌ FAILED   {doc.url}")
            print(f"   Error: {doc.error}")
        elif doc.file_type:
            print(f"\n✅ DOWNLOAD {doc.url}  ({doc.file_type}, {len(doc.content)} bytes)")
        else:
            print(f"\n✅ CRAWLED  {doc.url}")
            print(f"   File : {path}")

    print("\n" + "=" * 80)
    print(f"Total documents: {len(docs)}")
    print(f"Output directory: {Path('./output').resolve()}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import CrawlConfig, CrawlMode
from .crawler import crawl_confirmed, discover
from .io_output import save_all


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.crawler.main",
        description=(
            "Crawl a site (sitemap-first discovery, BFS fallback) and dump "
            "rendered markdown to ./output."
        ),
    )
    parser.add_argument("url", help="Root URL to crawl")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="skip the interactive confirmation prompt and crawl immediately",
    )
    parser.add_argument(
        "--wait-strategy",
        choices=["fixed_timeout", "networkidle", "selector"],
        default="fixed_timeout",
        help=(
            "How long to wait for client-rendered pages before capturing HTML "
            "(decision 7). fixed_timeout (default) captures right after the load "
            "event and is unsuitable for SPAs; networkidle waits for the network "
            "to go quiet; selector waits for --wait-selector to appear."
        ),
    )
    parser.add_argument(
        "--wait-selector",
        default=None,
        metavar="SELECTOR",
        help="CSS selector to wait for; only meaningful with --wait-strategy selector.",
    )
    return parser.parse_args(argv)


def _config_for(url: str, args: argparse.Namespace) -> CrawlConfig:
    domain = urlsplit(url).netloc
    return CrawlConfig(
        mode=CrawlMode.SITE,
        allowed_domains=(domain,),
        wait_strategy=args.wait_strategy,
        wait_selector=args.wait_selector,
    )


def _counts(pages) -> tuple[int, int]:
    page_count = sum(1 for p in pages if p.kind == "PAGE")
    doc_count = sum(1 for p in pages if p.kind == "DOCUMENT")
    return page_count, doc_count


async def main():
    args = _parse_args()

    try:
        config = _config_for(args.url, args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    result = await discover(args.url, config)

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

    if not args.confirm:
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

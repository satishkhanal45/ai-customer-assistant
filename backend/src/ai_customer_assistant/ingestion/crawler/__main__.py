import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit
from .config import CrawlConfig, CrawlMode
from .crawler import Crawler
from .io_output import save_all

def _config_for(url: str, mode: CrawlMode) -> CrawlConfig:
    domain = urlsplit(url).netloc
    return CrawlConfig(mode=mode, allowed_domains=(domain,))

async def main():
    args = tuple(sys.argv[1:])
    mode = CrawlMode.SITE if "--site" in args else CrawlMode.PAGE
    urls = tuple(a for a in args if not a.startswith("--"))

    if not urls:
        print("usage: python -m ingestion.crawler <url> [--site]")
        return

    docs = await Crawler(_config_for(urls[0], mode)).crawl(urls[0])
    saved = save_all(docs, Path("./output"))

    tuple(print(f"saved: {p}") for p in saved)
    tuple(print(f"failed: {d.url} — {d.error}") for d in docs if d.error)

if __name__ == "__main__":
    asyncio.run(main())
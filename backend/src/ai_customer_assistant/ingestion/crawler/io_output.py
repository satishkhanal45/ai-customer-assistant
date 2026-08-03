from pathlib import Path
from .models import CrawlDocument

def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] or "index"

def save_document(doc: CrawlDocument, out_dir: Path) -> Path:
    path = out_dir / f"{_slug(doc.url)}.md"
    path.write_text(doc.markdown, encoding="utf-8")
    return path

def save_all(docs: tuple[CrawlDocument, ...], out_dir: Path) -> tuple[Path, ...]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return tuple(save_document(d, out_dir) for d in docs if d.error is None)
"""Measure retrieval quality against the live corpus, and set the threshold
from what it measures rather than from a guess (P2-2).

## Why

`DEFAULT_SIMILARITY_THRESHOLD` was 0.70 with nothing behind the number. An
absolute floor on normalised BGE cosine similarity is hard to pick by
intuition, because the scale is compressed: unrelated English text does not
score near zero, it scores in the 0.6-0.7 band, right where weakly-relevant
text scores. A floor chosen blind is therefore simultaneously too permissive
and too strict, and there was no way to tell which way it was wrong.

## What it does

Reads `tests/data/golden_retrieval.json` — questions the corpus genuinely
answers, each labelled with the source that holds the evidence, plus
*negative* questions it genuinely cannot answer. Ranks every question against
every live chunk, and reports:

  * recall@1 / recall@k and MRR over the positives — did the right document
    come back, and how near the top;
  * the score distribution of positives vs negatives — how far apart the two
    populations actually sit;
  * the threshold that best separates them, and what each candidate threshold
    would cost in answered-but-wrong versus refused-but-answerable.

It runs each variant twice, with and without the BGE query instruction, so
the effect of that change is measured rather than assumed.

Nothing here writes to the database.

## Running it

    make up                       # postgres must be live and populated
    cd backend
    POSTGRES_HOST=localhost POSTGRES_PORT=5433 \\
      .venv/bin/python scripts/calibrate_retrieval.py

Add `--json out.json` to keep the raw numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src" / "ai_customer_assistant"))
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from config import load_env  # noqa: E402

load_env()

GOLDEN_SET_PATH = BACKEND_ROOT / "tests" / "data" / "golden_retrieval.json"

# Thresholds to report a cost/benefit line for. Deliberately spans well below
# and well above the shipped default so the table shows the shape of the
# tradeoff, not just a neighbourhood of the current value.
CANDIDATE_THRESHOLDS = (0.50, 0.55, 0.60, 0.62, 0.65, 0.68, 0.70, 0.72, 0.75, 0.80)

# Relative margins to sweep. The risk a margin carries is the opposite of the
# floor's: it cannot let noise in, it can only cut a correct source that
# happened not to rank first, so the sweep reports exactly that.
CANDIDATE_MARGINS = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30)


@dataclass(frozen=True)
class Chunk:
    source_name: str
    chunk_index: int
    text: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class QueryOutcome:
    query: str
    expected_sources: tuple[str, ...]
    ranked: tuple[tuple[str, float], ...]  # (source_name, similarity), best first

    @property
    def top_score(self) -> float:
        return self.ranked[0][1] if self.ranked else 0.0

    def rank_of_first_expected(self) -> int | None:
        """1-indexed position of the first correct source, or None."""
        for position, (source, _) in enumerate(self.ranked, start=1):
            if source in self.expected_sources:
                return position
        return None


def _parse_embedding(raw: object) -> tuple[float, ...]:
    """A raw `text()` query has no result type, so pgvector's adapter never
    runs and the vector arrives as its literal text form. Mirrors
    `vector_search._parse_embedding`, which handles the same two shapes."""
    return tuple(json.loads(raw)) if isinstance(raw, str) else tuple(float(v) for v in raw)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def load_live_chunks() -> tuple[Chunk, ...]:
    """Every chunk vector search is allowed to return, under the same join
    contract `vector_search.py` enforces — current version, INDEXED, active
    source. Measuring against anything else would measure a corpus the app
    cannot actually retrieve from."""
    from sqlalchemy import text

    from db.engine import get_session_factory

    statement = text(
        """
        SELECT ks.source_name, ec.chunk_index, ec.text, ec.embedding
        FROM embedding_chunk ec
        JOIN knowledge_source_version ksv ON ksv.version_id = ec.version_id
        JOIN knowledge_source ks ON ks.source_id = ksv.source_id
        WHERE ksv.version_id = ks.current_version_id
          AND ksv.status = 'INDEXED'
          AND ks.is_active
        ORDER BY ks.source_name, ec.chunk_index
        """
    )
    async with get_session_factory()() as session:
        rows = (await session.execute(statement)).all()
    return tuple(
        Chunk(
            source_name=row.source_name,
            chunk_index=row.chunk_index,
            text=row.text,
            embedding=_parse_embedding(row.embedding),
        )
        for row in rows
    )


def rank(query_vector: Sequence[float], chunks: Sequence[Chunk], top_k: int) -> tuple[tuple[str, float], ...]:
    scored = sorted(
        ((c.source_name, cosine(query_vector, c.embedding)) for c in chunks),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return tuple(scored[:top_k])


def evaluate(
    embed, golden: dict, chunks: Sequence[Chunk], top_k: int
) -> tuple[list[QueryOutcome], list[QueryOutcome]]:
    positives = [
        QueryOutcome(
            query=item["query"],
            expected_sources=tuple(item["expected_sources"]),
            ranked=rank(embed(item["query"]), chunks, top_k),
        )
        for item in golden["positives"]
    ]
    negatives = [
        QueryOutcome(
            query=item["query"],
            expected_sources=(),
            ranked=rank(embed(item["query"]), chunks, top_k),
        )
        for item in golden["negatives"]
    ]
    return positives, negatives


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def summarise(positives: Sequence[QueryOutcome], negatives: Sequence[QueryOutcome], top_k: int) -> dict:
    ranks = [outcome.rank_of_first_expected() for outcome in positives]
    found = [r for r in ranks if r is not None]
    positive_scores = [o.top_score for o in positives]
    negative_scores = [o.top_score for o in negatives]

    table = []
    for threshold in CANDIDATE_THRESHOLDS:
        # A positive is "answerable" if its correct source clears the floor.
        answerable = sum(
            1
            for outcome in positives
            if any(score >= threshold for source, score in outcome.ranked if source in outcome.expected_sources)
        )
        # A negative "leaks" when anything at all clears the floor, because
        # the agent will then answer from irrelevant context instead of
        # admitting it does not know.
        leaked = sum(1 for outcome in negatives if outcome.top_score >= threshold)
        table.append(
            {
                "threshold": threshold,
                "positives_retrievable": answerable,
                "positives_total": len(positives),
                "negatives_leaked": leaked,
                "negatives_total": len(negatives),
            }
        )

    margin_table = []
    for margin in CANDIDATE_MARGINS:
        kept_correct = 0
        total_returned = 0
        for outcome in positives:
            if not outcome.ranked:
                continue
            floor = outcome.ranked[0][1] - margin
            survivors = [pair for pair in outcome.ranked if pair[1] >= floor]
            total_returned += len(survivors)
            if any(source in outcome.expected_sources for source, _ in survivors):
                kept_correct += 1
        margin_table.append(
            {
                "margin": margin,
                "positives_keeping_correct_source": kept_correct,
                "positives_total": len(positives),
                "mean_chunks_returned": total_returned / len(positives) if positives else 0.0,
            }
        )

    return {
        "margin_table": margin_table,
        "recall_at_1": sum(1 for r in ranks if r == 1) / len(positives) if positives else 0.0,
        f"recall_at_{top_k}": len(found) / len(positives) if positives else 0.0,
        "mrr": sum(1 / r for r in found) / len(positives) if positives else 0.0,
        "positive_top_score": {
            "min": min(positive_scores, default=float("nan")),
            "p10": percentile(positive_scores, 0.10),
            "median": percentile(positive_scores, 0.50),
            "max": max(positive_scores, default=float("nan")),
        },
        "negative_top_score": {
            "min": min(negative_scores, default=float("nan")),
            "median": percentile(negative_scores, 0.50),
            "p90": percentile(negative_scores, 0.90),
            "max": max(negative_scores, default=float("nan")),
        },
        "separation": percentile(positive_scores, 0.10) - percentile(negative_scores, 0.90),
        "threshold_table": table,
    }


def render_detail(positives: Sequence[QueryOutcome], negatives: Sequence[QueryOutcome]) -> None:
    """Per-question scores. Summary statistics tell you a threshold is wrong;
    only this tells you *which* questions it is wrong about."""
    print("\n  answerable questions (rank of correct source, its best score):")
    for outcome in sorted(positives, key=lambda o: o.top_score):
        position = outcome.rank_of_first_expected()
        best_correct = max(
            (score for source, score in outcome.ranked if source in outcome.expected_sources),
            default=0.0,
        )
        print(f"    {best_correct:.3f}  rank {position or '-':>2}   {outcome.query}")
    print("\n  unanswerable questions (best score from anything):")
    for outcome in sorted(negatives, key=lambda o: -o.top_score):
        print(f"    {outcome.top_score:.3f}  {outcome.ranked[0][0][:38]:<38}  {outcome.query}")


def render(name: str, summary: dict, top_k: int) -> None:
    print(f"\n=== {name} " + "=" * max(0, 58 - len(name)))
    print(
        f"  recall@1 {summary['recall_at_1']:.2%}   "
        f"recall@{top_k} {summary[f'recall_at_{top_k}']:.2%}   "
        f"MRR {summary['mrr']:.3f}"
    )
    p, n = summary["positive_top_score"], summary["negative_top_score"]
    print(f"  answerable questions, top score: min {p['min']:.3f}  p10 {p['p10']:.3f}  median {p['median']:.3f}  max {p['max']:.3f}")
    print(f"  unanswerable questions, top score: min {n['min']:.3f}  median {n['median']:.3f}  p90 {n['p90']:.3f}  max {n['max']:.3f}")
    print(f"  separation (positive p10 - negative p90): {summary['separation']:+.3f}")
    print("\n  threshold   answerable kept   unanswerable leaked")
    for row in summary["threshold_table"]:
        print(
            f"     {row['threshold']:.2f}      "
            f"{row['positives_retrievable']:>3}/{row['positives_total']:<3}            "
            f"{row['negatives_leaked']:>3}/{row['negatives_total']}"
        )
    print("\n  margin   correct source survives   mean chunks returned")
    for row in summary["margin_table"]:
        print(
            f"    {row['margin']:.2f}         "
            f"{row['positives_keeping_correct_source']:>3}/{row['positives_total']:<3}                "
            f"{row['mean_chunks_returned']:.1f}"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--detail", action="store_true", help="print per-question scores")
    parser.add_argument("--json", type=Path, help="write the raw numbers here")
    args = parser.parse_args()

    golden = json.loads(GOLDEN_SET_PATH.read_text())
    chunks = await load_live_chunks()
    if not chunks:
        print("No live INDEXED chunks. Ingest something first (see README).", file=sys.stderr)
        return 1

    known_sources = {c.source_name for c in chunks}
    expected = {s for item in golden["positives"] for s in item["expected_sources"]}
    missing = sorted(expected - known_sources)
    if missing:
        print(
            "WARNING: the golden set expects sources that are not in the live "
            f"corpus, so its recall numbers understate reality: {missing}",
            file=sys.stderr,
        )

    print(f"corpus: {len(chunks)} live chunks across {len(known_sources)} sources")
    print(f"golden set: {len(golden['positives'])} answerable, {len(golden['negatives'])} unanswerable")

    from agents.knowledge.constants import BGE_QUERY_INSTRUCTION
    from services.embeddings import build_shared_embeddings

    results = {}
    for name, instruction in (
        ("without the BGE query instruction (previous behaviour)", ""),
        ("with the BGE query instruction", BGE_QUERY_INSTRUCTION),
    ):
        embeddings = build_shared_embeddings(query_instruction=instruction)
        positives, negatives = evaluate(embeddings.embed_query, golden, chunks, args.top_k)
        summary = summarise(positives, negatives, args.top_k)
        results[name] = summary
        render(name, summary, args.top_k)
        if args.detail:
            render_detail(positives, negatives)

    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

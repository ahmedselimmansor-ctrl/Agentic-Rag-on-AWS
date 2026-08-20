"""pgvector latency under load.

Answers the question you actually need before provisioning: at what corpus size
does vector search stop being fast, and does the HNSW index still get used?

Embeddings are synthetic — random unit vectors — because this measures the
*index*, not relevance. Using real embeddings would make the benchmark cost
money and depend on a provider being up, without changing what it measures.

    python -m bench.bench_retrieval --rows 100000 --queries 200
    python -m bench.bench_retrieval --rows 1000000 --ef-search 40,100,200
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import statistics
import time
import uuid

from sqlalchemy import text

from app.config import settings
from app.db.session import engine, session_scope

BENCH_USER = uuid.UUID("00000000-0000-0000-0000-0000000000be")
BENCH_DOC = uuid.UUID("00000000-0000-0000-0000-0000000000d0")


def random_unit_vector(dim: int, rng: random.Random) -> str:
    values = [rng.gauss(0, 1) for _ in range(dim)]
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return "[" + ",".join(f"{v / norm:.6f}" for v in values) + "]"


async def seed(rows: int, batch: int = 1000) -> None:
    dim = settings.embedding_dim
    rng = random.Random(1234)  # noqa: S311 - reproducible, not cryptographic

    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO users (id, email) VALUES (:id, 'bench@local')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": str(BENCH_USER)},
        )
        await session.execute(
            text(
                """
                INSERT INTO documents (id, user_id, filename, storage_uri, status, sha256)
                VALUES (:id, :user_id, 'bench.txt', 'file:///dev/null', 'ready', 'bench')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": str(BENCH_DOC), "user_id": str(BENCH_USER)},
        )
        existing = (
            await session.execute(
                text("SELECT count(*) FROM chunks WHERE document_id = :d"),
                {"d": str(BENCH_DOC)},
            )
        ).scalar_one()

    if existing >= rows:
        print(f"  {existing:,} bench rows already present")
        # ANALYZE regardless. Stale statistics make the planner fall back to a
        # sequential scan, which measures the wrong thing entirely — and that
        # happens silently unless you read the plan.
        await _analyze()
        return

    print(f"  seeding {rows - existing:,} rows (dim={dim})…")
    started = time.perf_counter()
    ordinal = existing

    while ordinal < rows:
        size = min(batch, rows - ordinal)
        values = []
        params: dict[str, object] = {"doc": str(BENCH_DOC), "user": str(BENCH_USER)}
        for i in range(size):
            params[f"o{i}"] = ordinal + i
            params[f"c{i}"] = f"synthetic passage {ordinal + i} about topic {(ordinal + i) % 997}"
            params[f"e{i}"] = random_unit_vector(dim, rng)
            # CAST(...) not '::vector' — see the note in services/retrieval.py.
            values.append(f"(:doc, :user, :o{i}, :c{i}, CAST(:e{i} AS vector))")

        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO chunks (document_id, user_id, ordinal, content, embedding) "
                    f"VALUES {','.join(values)} ON CONFLICT DO NOTHING"
                ),
                params,
            )
        ordinal += size
        if ordinal % 20000 < batch:
            print(f"    {ordinal:,}/{rows:,}")

    print(f"  seeded in {time.perf_counter() - started:.1f}s")
    await _analyze()


async def _analyze() -> None:
    async with session_scope() as session:
        await session.execute(text("ANALYZE chunks"))


async def measure(queries: int, k: int, ef_search: int | None) -> dict[str, float]:
    dim = settings.embedding_dim
    rng = random.Random(99)  # noqa: S311
    latencies: list[float] = []

    async with session_scope() as session:
        if ef_search:
            await session.execute(text(f"SET hnsw.ef_search = {int(ef_search)}"))

        for _ in range(queries):
            vector = random_unit_vector(dim, rng)
            started = time.perf_counter()
            await session.execute(
                text(
                    """
                    SELECT c.id FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.embedding IS NOT NULL AND d.status = 'ready'
                      AND c.user_id = :user
                    ORDER BY c.embedding <=> CAST(:qvec AS vector)
                    LIMIT :k
                    """
                ),
                {"qvec": vector, "k": k, "user": str(BENCH_USER)},
            )
            latencies.append((time.perf_counter() - started) * 1000)

    latencies.sort()
    return {
        "p50": statistics.median(latencies),
        "p95": latencies[int(len(latencies) * 0.95) - 1],
        "p99": latencies[int(len(latencies) * 0.99) - 1],
        "mean": statistics.mean(latencies),
        "max": latencies[-1],
    }


async def explain(k: int) -> str:
    """Confirm the HNSW index is actually being used. A sequential scan here is
    the difference between milliseconds and seconds, and it is silent."""
    rng = random.Random(7)  # noqa: S311
    vector = random_unit_vector(settings.embedding_dim, rng)
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM chunks "
                    "WHERE embedding IS NOT NULL AND user_id = :user "
                    "ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :k"
                ),
                {"qvec": vector, "k": k, "user": str(BENCH_USER)},
            )
        ).scalars().all()

    # EXPLAIN echoes the full query vector — 1024 floats of noise that buries
    # the one line you are here to read.
    trimmed = []
    for row in rows:
        line = re.sub(r"'\[[-0-9.,e ]{40,}\]'", "'[…]'", str(row))
        trimmed.append(f"      {line[:200]}")
    return "\n".join(trimmed)


async def cleanup() -> None:
    async with session_scope() as session:
        await session.execute(
            text("DELETE FROM chunks WHERE document_id = :d"), {"d": str(BENCH_DOC)}
        )
        await session.execute(text("DELETE FROM documents WHERE id = :d"), {"d": str(BENCH_DOC)})
        await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": str(BENCH_USER)})
    print("  bench data removed")


async def main() -> None:
    parser = argparse.ArgumentParser(description="pgvector retrieval benchmark")
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("-k", type=int, default=10)
    parser.add_argument("--ef-search", default="", help="comma-separated values to sweep")
    parser.add_argument("--explain", action="store_true", help="show the query plan")
    parser.add_argument("--cleanup", action="store_true", help="delete bench rows and exit")
    args = parser.parse_args()

    if args.cleanup:
        await cleanup()
        await engine.dispose()
        return

    print(f"\n  pgvector benchmark — {args.rows:,} rows, {args.queries} queries, k={args.k}")
    print("  " + "─" * 58)
    await seed(args.rows)

    settings_list = [int(v) for v in args.ef_search.split(",") if v.strip()] or [None]

    print(f"\n  {'ef_search':>10} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}")
    for ef in settings_list:
        stats = await measure(args.queries, args.k, ef)
        label = str(ef) if ef else "default"
        print(
            f"  {label:>10} {stats['p50']:>8.1f}ms {stats['p95']:>8.1f}ms "
            f"{stats['p99']:>8.1f}ms {stats['max']:>8.1f}ms"
        )

    if args.explain:
        print("\n  QUERY PLAN")
        print(await explain(args.k))
        print(
            "\n  'Index Scan using ix_chunks_embedding_hnsw' means the index is in use.\n"
            "  A Seq Scan is not automatically wrong: below roughly 50k rows the\n"
            "  planner often judges a scan-and-sort cheaper than HNSW's overhead,\n"
            "  and it is usually right. It only signals a problem at scale, or when\n"
            "  latency is far worse than the numbers above."
        )

    print("\n  Remove the synthetic rows with --cleanup\n")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

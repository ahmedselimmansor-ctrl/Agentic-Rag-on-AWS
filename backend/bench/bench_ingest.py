"""Ingestion throughput, broken down by stage.

A single "took 40 seconds" number tells you nothing actionable. Parse, chunk,
and embed fail and scale for completely different reasons — embedding is
provider latency you can only parallelise, parsing is CPU you own, and a
parse-dominated profile usually means the document is a scan going through OCR.

    python -m bench.bench_ingest --file doc.pdf --repeat 3
    python -m bench.bench_ingest --file doc.pdf --no-embed   # skip provider cost
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.db.session import engine
from app.services.chunking import chunk_document
from app.services.embeddings import EmbedInput, embed
from app.services.parsing import parse


@dataclass
class Timing:
    parse_ms: float
    chunk_ms: float
    embed_ms: float
    blocks: int
    chunks: int
    tokens: int

    @property
    def total_ms(self) -> float:
        return self.parse_ms + self.chunk_ms + self.embed_ms


async def run_once(path: Path, mime: str, do_embed: bool) -> Timing:
    started = time.perf_counter()
    parsed = parse(str(path), mime)
    parse_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    drafts = chunk_document(parsed)
    chunk_ms = (time.perf_counter() - started) * 1000

    embed_ms = 0.0
    if do_embed and drafts:
        started = time.perf_counter()
        await embed([EmbedInput(text=d.embed_text) for d in drafts])
        embed_ms = (time.perf_counter() - started) * 1000

    return Timing(
        parse_ms=parse_ms,
        chunk_ms=chunk_ms,
        embed_ms=embed_ms,
        blocks=len(parsed.blocks),
        chunks=len(drafts),
        tokens=sum(d.token_count for d in drafts),
    )


def _row(label: str, values: list[float], total: float) -> str:
    mean = statistics.mean(values)
    share = (mean / total * 100) if total else 0
    bar = "█" * int(round(share / 4))
    return f"    {label:<8} {mean:>9.0f}ms  {share:>5.1f}%  {bar}"


async def run(args: argparse.Namespace, path: Path, mime: str, size_kb: float) -> None:
    print(f"\n  {path.name}  —  {size_kb:.0f} KB  —  {mime}")
    if not args.no_embed:
        print(f"  embedding via {settings.embedding_model} (dim {settings.embedding_dim})")
    print("  " + "─" * 58)

    timings: list[Timing] = []
    for i in range(args.repeat):
        try:
            timing = await run_once(path, mime, not args.no_embed)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"  run {i + 1} failed: {exc}") from exc
        timings.append(timing)
        print(f"    run {i + 1}: {timing.total_ms:>8.0f}ms   {timing.chunks} chunks")

    first = timings[0]
    total = statistics.mean(t.total_ms for t in timings)

    print(f"\n  {first.blocks} blocks -> {first.chunks} chunks, {first.tokens:,} tokens")
    print(f"\n  STAGE BREAKDOWN  (mean of {args.repeat})")
    print(_row("parse", [t.parse_ms for t in timings], total))
    print(_row("chunk", [t.chunk_ms for t in timings], total))
    if not args.no_embed:
        print(_row("embed", [t.embed_ms for t in timings], total))

    print(f"\n    {'total':<8} {total:>9.0f}ms")
    if first.tokens:
        print(f"    {'per 1k tok':<8} {total / (first.tokens / 1000):>9.0f}ms")

    parse_share = statistics.mean(t.parse_ms for t in timings) / total if total else 0
    if parse_share > 0.5:
        print(
            "\n  Parsing dominates — this is usually a scanned document going\n"
            "  through OCR, or a very large table-heavy file."
        )
    elif not args.no_embed:
        print(
            "\n  Embedding usually dominates and is provider latency, not your CPU.\n"
            f"  Raise EMBEDDING_BATCH_SIZE (currently {settings.embedding_batch_size})\n"
            "  to send fewer, larger requests."
        )

    print()
    await engine.dispose()


def main() -> None:
    """Argument parsing and filesystem checks stay synchronous — blocking
    syscalls do not belong inside the event loop, even in a benchmark."""
    import mimetypes

    parser = argparse.ArgumentParser(description="ingestion benchmark")
    parser.add_argument("--file", required=True)
    parser.add_argument("--mime", default="", help="override the detected MIME type")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--no-embed", action="store_true", help="skip the embedding provider")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"  no such file: {path}")

    mime = args.mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    size_kb = path.stat().st_size / 1024

    asyncio.run(run(args, path, mime, size_kb))


if __name__ == "__main__":
    main()

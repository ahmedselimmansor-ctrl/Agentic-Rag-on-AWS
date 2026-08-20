"""Concurrent SSE load.

Streaming turns hold a connection open for the whole answer, so the metric that
matters is not requests-per-second — it is how many *simultaneous* streams a
task sustains, and whether time-to-first-token degrades as they pile up.

Time-to-first-token is reported separately from total duration because they
fail differently: rising TTFT means the server is saturated before generation
starts; rising total with flat TTFT means the model is simply slower.

    python -m bench.bench_stream --url http://localhost:8000 \
        --email you@example.com --password '...' --concurrency 20 --requests 100
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Result:
    ok: bool
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    tokens: int = 0
    status: int = 0
    error: str = ""


@dataclass
class Summary:
    results: list[Result] = field(default_factory=list)

    @property
    def ok(self) -> list[Result]:
        return [r for r in self.results if r.ok]

    def percentiles(self, values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        ordered = sorted(values)
        return {
            "p50": statistics.median(ordered),
            "p95": ordered[max(0, int(len(ordered) * 0.95) - 1)],
            "p99": ordered[max(0, int(len(ordered) * 0.99) - 1)],
            "max": ordered[-1],
        }


async def authenticate(client: httpx.AsyncClient, base: str, email: str, password: str) -> str:
    response = await client.post(
        f"{base}/api/auth/login", json={"email": email, "password": password}
    )
    if response.status_code != 200:
        raise SystemExit(
            f"  login failed ({response.status_code}): {response.text[:200]}\n"
            "  Register the account first, or pass --token."
        )
    return response.json()["access_token"]


async def one_stream(
    client: httpx.AsyncClient, base: str, token: str, message: str, read_timeout: float
) -> Result:
    started = time.perf_counter()
    first_token_at: float | None = None
    tokens = 0

    try:
        async with client.stream(
            "POST",
            f"{base}/api/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": message, "web_search": False},
            timeout=read_timeout,
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode()[:150]
                return Result(ok=False, status=response.status_code, error=body)

            async for line in response.aiter_lines():
                if line.startswith("event: token"):
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    tokens += 1
                elif line.startswith("event: error"):
                    return Result(ok=False, status=200, error="stream reported an error")
    except Exception as exc:  # noqa: BLE001
        return Result(ok=False, error=f"{type(exc).__name__}: {exc}"[:150])

    total = (time.perf_counter() - started) * 1000
    return Result(
        ok=True,
        ttft_ms=((first_token_at - started) * 1000) if first_token_at else total,
        total_ms=total,
        tokens=tokens,
    )


async def run(args: argparse.Namespace) -> None:
    base = args.url.rstrip("/")
    limits = httpx.Limits(max_connections=args.concurrency + 10)

    async with httpx.AsyncClient(limits=limits) as client:
        token = args.token or await authenticate(client, base, args.email, args.password)

        semaphore = asyncio.Semaphore(args.concurrency)
        summary = Summary()

        async def guarded(index: int) -> None:
            async with semaphore:
                message = f"{args.message} (request {index})"
                summary.results.append(
                    await one_stream(client, base, token, message, args.timeout)
                )

        print(
            f"\n  {args.requests} streams, {args.concurrency} concurrent, "
            f"target {base}"
        )
        print("  " + "─" * 58)

        started = time.perf_counter()
        await asyncio.gather(*(guarded(i) for i in range(args.requests)))
        wall = time.perf_counter() - started

    ok = summary.ok
    failed = [r for r in summary.results if not r.ok]

    print(f"\n  completed  {len(ok)}/{len(summary.results)}   in {wall:.1f}s")
    if ok:
        print(f"  throughput {len(ok) / wall:.2f} streams/s")

        ttft = summary.percentiles([r.ttft_ms for r in ok])
        total = summary.percentiles([r.total_ms for r in ok])
        print(f"\n  {'':<22}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}")
        print(
            f"  {'time to first token':<22}{ttft['p50']:>8.0f}ms{ttft['p95']:>8.0f}ms"
            f"{ttft['p99']:>8.0f}ms{ttft['max']:>8.0f}ms"
        )
        print(
            f"  {'total':<22}{total['p50']:>8.0f}ms{total['p95']:>8.0f}ms"
            f"{total['p99']:>8.0f}ms{total['max']:>8.0f}ms"
        )
        print(f"\n  mean tokens/answer  {statistics.mean(r.tokens for r in ok):.0f}")

    if failed:
        print(f"\n  FAILURES ({len(failed)})")
        seen: dict[str, int] = {}
        for result in failed:
            key = f"{result.status or '-'} {result.error[:80]}"
            seen[key] = seen.get(key, 0) + 1
        for key, count in sorted(seen.items(), key=lambda kv: -kv[1])[:6]:
            print(f"    {count:>4}x  {key}")

    print(
        "\n  Rising TTFT under concurrency = the server saturates before generation.\n"
        "  Flat TTFT with rising total   = the model is the bottleneck, not you.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="concurrent SSE benchmark")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--token", default="", help="skip login and use this access token")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--message", default="Summarise the key points in one paragraph.")
    args = parser.parse_args()

    if not args.token and not (args.email and args.password):
        raise SystemExit("  need --token, or --email and --password")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()

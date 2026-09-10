"""
A tiny async load generator (Stage 12) — no framework, just asyncio + httpx.

Fires N job submissions at a chosen concurrency and reports throughput, latency
percentiles, and the status-code split — the client-side view you compare against
the server's SLO (/v1/admin/slo). Pair it with the chaos injector to watch p95
and error rate move.

Usage:
  python scripts/loadtest.py --url http://localhost:8000 --key csk_... --n 500 --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter

import httpx


async def _worker(client, url, key, queue, latencies, statuses):
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        start = time.perf_counter()
        try:
            r = await client.post(
                f"{url}/v1/jobs",
                headers={"Authorization": f"Bearer {key}"},
                json={"payload": {"prompt": "load test"}},
            )
            statuses[r.status_code] += 1
        except Exception as exc:  # noqa: BLE001
            statuses[f"err:{type(exc).__name__}"] += 1
        finally:
            latencies.append((time.perf_counter() - start) * 1000.0)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100.0 * len(s)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", required=True, help="API key (csk_...)")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()

    queue: asyncio.Queue = asyncio.Queue()
    for _ in range(args.n):
        queue.put_nowait(1)

    latencies: list[float] = []
    statuses: Counter = Counter()

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*[
            _worker(client, args.url, args.key, queue, latencies, statuses)
            for _ in range(args.concurrency)
        ])
    elapsed = time.perf_counter() - started

    print(f"requests   : {args.n} at concurrency {args.concurrency}")
    print(f"wall time  : {elapsed:.2f}s   throughput: {args.n / elapsed:.1f} req/s")
    print(f"latency ms : p50={_pct(latencies,50):.1f}  p95={_pct(latencies,95):.1f}  p99={_pct(latencies,99):.1f}  max={max(latencies):.1f}")
    print(f"statuses   : {dict(statuses)}")


if __name__ == "__main__":
    asyncio.run(main())

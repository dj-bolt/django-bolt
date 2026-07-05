#!/usr/bin/env python3
"""HTTP QUERY method load generator.

bombardier (the tool the other benchmarks use) rejects non-standard methods
with "Unknown HTTP method: QUERY", so QUERY throughput is measured here with
httpx instead — which is already a django-bolt dependency and sends arbitrary
methods. The same generator drives POST for an apples-to-apples comparison
against the byte-identical /bench/parse endpoint.

Output mirrors the "Reqs/sec / Latency / NN%" lines the shell benchmarks emit
so it can be grepped the same way.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx

DEFAULT_BODY = '{"title":"bench","count":100,"items":[{"name":"a","price":1.0,"is_offer":true}]}'


def _percentile(sorted_us: list[float], pct: float) -> float:
    """Nearest-rank percentile over already-sorted microsecond latencies."""
    if not sorted_us:
        return 0.0
    rank = max(0, min(len(sorted_us) - 1, round(pct / 100.0 * len(sorted_us)) - 1))
    return sorted_us[rank]


async def _worker(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    body: bytes,
    count: int,
    latencies: list[float],
    errors: list[int],
) -> None:
    headers = {"Content-Type": "application/json"}
    for _ in range(count):
        start = time.perf_counter()
        try:
            resp = await client.request(method, url, content=body, headers=headers)
            elapsed_us = (time.perf_counter() - start) * 1_000_000
            if resp.status_code == 200:
                latencies.append(elapsed_us)
            else:
                errors[0] += 1
        except Exception:
            errors[0] += 1


async def run(method: str, url: str, body: bytes, concurrency: int, total: int, warmup: int) -> None:
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
        # Warmup (not measured): primes connections and the JIT-y first-call paths.
        if warmup > 0:
            warm_lat: list[float] = []
            warm_err = [0]
            await _worker(client, method, url, body, warmup, warm_lat, warm_err)

        latencies: list[float] = []
        errors = [0]
        per_worker = [total // concurrency] * concurrency
        for i in range(total % concurrency):  # spread the remainder
            per_worker[i] += 1

        wall_start = time.perf_counter()
        await asyncio.gather(*(_worker(client, method, url, body, n, latencies, errors) for n in per_worker if n > 0))
        wall = time.perf_counter() - wall_start

    done = len(latencies)
    rps = done / wall if wall > 0 else 0.0
    latencies.sort()
    avg_us = sum(latencies) / done if done else 0.0

    print(f"  Reqs/sec  {rps:12.2f}")
    print(f"  Latency   {avg_us / 1000:8.2f}ms  (avg)")
    print(f"    50%     {_percentile(latencies, 50) / 1000:8.2f}ms")
    print(f"    75%     {_percentile(latencies, 75) / 1000:8.2f}ms")
    print(f"    90%     {_percentile(latencies, 90) / 1000:8.2f}ms")
    print(f"    99%     {_percentile(latencies, 99) / 1000:8.2f}ms")
    print(f"  Completed {done}/{total}  Errors {errors[0]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP QUERY/POST load generator (httpx)")
    parser.add_argument("url", help="Target URL, e.g. http://127.0.0.1:8001/bench/query")
    parser.add_argument("-m", "--method", default="QUERY", help="HTTP method (default: QUERY)")
    parser.add_argument("-c", "--concurrency", type=int, default=50, help="Concurrent connections (default: 50)")
    parser.add_argument("-n", "--requests", type=int, default=10000, help="Total requests (default: 10000)")
    parser.add_argument("--warmup", type=int, default=200, help="Unmeasured warmup requests (default: 200)")
    parser.add_argument("--body", default=DEFAULT_BODY, help="JSON request body")
    args = parser.parse_args()

    asyncio.run(
        run(
            method=args.method.upper(),
            url=args.url,
            body=args.body.encode(),
            concurrency=args.concurrency,
            total=args.requests,
            warmup=args.warmup,
        )
    )


if __name__ == "__main__":
    main()

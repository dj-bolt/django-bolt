"""Benchmark micro-reflex event round-trips over a real runbolt server.

Usage: bench_micro.py <conns> <rounds_per_conn> [label]
Starts its own server (repo root taken from CWD), prints one JSON line.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import websockets

CONNS = int(sys.argv[1]) if len(sys.argv) > 1 else 1
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 200
LABEL = sys.argv[3] if len(sys.argv) > 3 else "micro-reflex"
SCRATCH = Path(__file__).parent

from tests.integration.apps import app_module  # noqa: E402
from tests.integration.helpers import create_server_project  # noqa: E402

EVENT = json.dumps({"t": "e", "h": {"s": "DemoCounterState", "n": "increment"}})


async def run_conn(url: str, latencies: list[float], barrier) -> None:
    async with websockets.connect(url, open_timeout=10) as ws:
        # Warmup round.
        await ws.send(EVENT)
        await asyncio.wait_for(ws.recv(), timeout=10)
        await barrier.wait()
        for _ in range(ROUNDS):
            start = time.perf_counter()
            await ws.send(EVENT)
            await asyncio.wait_for(ws.recv(), timeout=10)
            latencies.append(time.perf_counter() - start)


async def bench(host: str, port: int) -> None:
    url = f"ws://{host}:{port}/_rx/ws?page=/"
    latencies: list[float] = []
    start_holder = {}
    real_barrier = asyncio.Barrier(CONNS)

    class _B:
        async def wait(self):
            await real_barrier.wait()
            start_holder.setdefault("t", time.perf_counter())

    b = _B()
    await asyncio.gather(*(run_conn(url, latencies, b) for _ in range(CONNS)))
    elapsed = time.perf_counter() - start_holder["t"]
    latencies.sort()
    n = len(latencies)
    print(
        json.dumps(
            {
                "framework": LABEL,
                "conns": CONNS,
                "rounds": n,
                "throughput_rps": round(n / elapsed, 1),
                "p50_us": round(statistics.median(latencies) * 1e6),
                "p90_us": round(latencies[int(n * 0.90)] * 1e6),
                "p99_us": round(latencies[int(n * 0.99)] * 1e6),
                "mean_us": round(statistics.fmean(latencies) * 1e6),
            }
        )
    )


def main() -> None:
    with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
        project = create_server_project(Path(tmp) / "proj", api_module=app_module("microreflex_demo"))
        with project.start() as server:
            asyncio.run(bench(server.host, server.port))


main()

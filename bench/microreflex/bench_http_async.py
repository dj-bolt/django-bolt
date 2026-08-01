"""Benchmark async HTTP dispatch over a real runbolt server.

Usage: bench_http.py <conns> <rounds_per_conn> <label>
Run with CWD at the repo root under test (PYTHONPATH=python).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

CONNS = int(sys.argv[1]) if len(sys.argv) > 1 else 1
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 500
LABEL = sys.argv[3] if len(sys.argv) > 3 else "http"

APP = os.environ.get("BENCH_APP", "async_probe_bench")
PATH = os.environ.get("BENCH_PATH", "/probe")
SCRATCH = Path(__file__).parent
STALLS: list[int] = []

from tests.integration.apps import app_module  # noqa: E402
from tests.integration.helpers import create_server_project  # noqa: E402


async def _request(reader, writer, raw: bytes) -> None:
    writer.write(raw)
    await writer.drain()
    # Read status + headers.
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    assert b" 200 " in header.split(b"\r\n", 1)[0], header[:80]
    length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length:
        await asyncio.wait_for(reader.readexactly(length), timeout=15)
    assert b"connection: close" not in header.lower(), "server closed keep-alive"


async def run_conn(base: str, latencies: list[float], barrier) -> None:
    host, port = base.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    raw = (f"GET {PATH} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: keep-alive\r\n\r\n").encode()
    stalled = False
    try:
        await _request(reader, writer, raw)  # warmup
        await barrier.wait()
        for _ in range(ROUNDS):
            start = time.perf_counter()
            await _request(reader, writer, raw)
            latencies.append(time.perf_counter() - start)
    except (TimeoutError, asyncio.IncompleteReadError) as exc:
        stalled = True
        print(f"# conn stalled after {len(latencies)} rounds: {type(exc).__name__}", file=sys.stderr)
    finally:
        writer.close()
    if stalled:
        STALLS.append(1)


async def bench(host: str, port: int) -> None:
    base = f"http://{host}:{port}"
    latencies: list[float] = []
    start_holder: dict[str, float] = {}
    real_barrier = asyncio.Barrier(CONNS)

    class _B:
        async def wait(self):
            await real_barrier.wait()
            start_holder.setdefault("t", time.perf_counter())

    b = _B()
    await asyncio.gather(*(run_conn(base, latencies, b) for _ in range(CONNS)))
    elapsed = time.perf_counter() - start_holder["t"]
    latencies.sort()
    n = len(latencies)
    print(
        json.dumps(
            {
                "framework": LABEL,
                "conns": CONNS,
                "rounds": n,
                "stalled_conns": len(STALLS),
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
        project = create_server_project(Path(tmp) / "proj", api_module=app_module(APP))
        with project.start() as server:
            asyncio.run(bench(server.host, server.port))


main()

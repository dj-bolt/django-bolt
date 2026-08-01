"""Benchmark real Reflex backend event round-trips over its socket.io protocol.

Usage: bench_reflex.py <port> <conns> <rounds_per_conn>
Prints one JSON line with latency percentiles and throughput.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import uuid

import socketio

sys.path.insert(0, os.environ.get("REFLEX_PROJECT", "./counter_project"))
from counter.counter import CounterState  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
CONNS = int(sys.argv[2]) if len(sys.argv) > 2 else 1
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 200

NAMESPACE = "/_event"
EVENT_NAME = f"{CounterState.get_full_name()}.increment"

ROUTER_DATA = {
    "pathname": "/",
    "query": {},
    "token": "",
    "sid": "",
    "headers": {},
    "ip": "127.0.0.1",
}


async def run_conn(latencies: list[float], barrier: asyncio.Barrier) -> None:
    token = str(uuid.uuid4())
    sio = socketio.AsyncClient(logger=False, engineio_logger=False)
    responded: asyncio.Queue = asyncio.Queue()

    @sio.on("event", namespace=NAMESPACE)
    async def on_event(data):  # noqa: ANN001
        await responded.put(data)

    await sio.connect(
        f"http://127.0.0.1:{PORT}?token={token}",
        socketio_path="/_event",
        namespaces=[NAMESPACE],
        transports=["websocket"],
    )

    router_data = dict(ROUTER_DATA, token=token)
    payload = {
        "token": token,
        "name": EVENT_NAME,
        "router_data": router_data,
        "payload": {},
    }

    # Warmup: first event instantiates the per-token state tree.
    await sio.emit("event", payload, namespace=NAMESPACE)
    await asyncio.wait_for(responded.get(), timeout=10)

    await barrier.wait()
    for _ in range(ROUNDS):
        start = time.perf_counter()
        await sio.emit("event", payload, namespace=NAMESPACE)
        await asyncio.wait_for(responded.get(), timeout=10)
        latencies.append(time.perf_counter() - start)

    await sio.disconnect()


async def main() -> None:
    latencies: list[float] = []
    start_holder = {}
    stamp_barrier = asyncio.Barrier(CONNS)

    class _B:
        async def wait(self):
            await stamp_barrier.wait()
            start_holder.setdefault("t", time.perf_counter())

    b = _B()
    await asyncio.gather(*(run_conn(latencies, b) for _ in range(CONNS)))
    elapsed = time.perf_counter() - start_holder["t"]
    latencies.sort()
    n = len(latencies)
    print(
        json.dumps(
            {
                "framework": "reflex",
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


asyncio.run(main())

"""Measure JSON contention with shared or private payloads and encoders."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import msgspec

p = argparse.ArgumentParser()
p.add_argument("--threads", type=int, default=12)
p.add_argument(
    "--mode",
    choices=["shared", "local-encoder", "local-data", "local-both", "function", "function-local"],
    default="shared",
)
p.add_argument("--seconds", type=float, default=1.5)
a = p.parse_args()
if a.threads < 1:
    p.error("--threads must be 1 or more")
raw = (Path(__file__).resolve().parents[1] / "example/test_data/10K.json").read_bytes()
payload = msgspec.json.decode(raw)
encoder = msgspec.json.Encoder()
barrier = threading.Barrier(a.threads + 1)
results = [None] * a.threads
end = 0


def work(i):
    data = msgspec.json.decode(raw) if a.mode in ("local-data", "local-both", "function-local") else payload
    enc = msgspec.json.Encoder() if a.mode in ("local-encoder", "local-both") else encoder
    encode = msgspec.json.encode if a.mode.startswith("function") else enc.encode
    for _ in range(100):
        encode(data)
    barrier.wait()
    count = 0
    cpu = time.thread_time()
    while time.perf_counter() < end:
        for _ in range(100):
            encode(data)
        count += 100
    results[i] = (count, time.thread_time() - cpu)


threads = [threading.Thread(target=work, args=(i,)) for i in range(a.threads)]
for t in threads:
    t.start()
start = time.perf_counter()
end = start + a.seconds
barrier.wait()
for t in threads:
    t.join()
elapsed = time.perf_counter() - start
print(
    json.dumps(
        {
            "python": sys.version.split()[0],
            "gil": getattr(sys, "_is_gil_enabled", lambda: True)(),
            "mode": a.mode,
            "threads": a.threads,
            "rps": round(sum(x[0] for x in results) / elapsed),
            "cpu_cores": round(sum(x[1] for x in results) / elapsed, 2),
            "seconds": round(elapsed, 3),
            "payload_bytes": len(encoder.encode(payload)),
        }
    )
)

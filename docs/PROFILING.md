# Profiling & Benchmarking Guide

Django-Bolt has a layered measurement strategy. Different changes are visible at
different altitudes — use the right one for the change you're making:

| Layer | Tool | Proves | Command |
|---|---|---|---|
| Macro HTTP | bombardier | End-to-end RPS + latency distribution | `just save-bench` |

Suggested proof discipline per optimization PR: `just save-bench` shows the
win survives end-to-end → a before/after flamegraph pair as the narrative
artifact.

## Dispatch-path probes (async bridge cost)

`python/tests/integration/apps/dispatch_probes.py` has one route per dispatch
mechanism (`/t-sync`, `/t-trivial`, `/t-ready`, `/t-sleep0`, `/t-thread`, …).
Point a `runbolt` server at it (`BOLT_API = ["tests.integration.apps.dispatch_probes:api"]`)
and compare per-request latency over a keepalive connection:

- `t_ready − t_trivial` = pure async-bridge cost (no real suspension)
- `t_sleep0 − t_ready` = one suspend/resume cycle

Measure worker-local results at both C=1 and high concurrency with server and
load-generator CPU affinity. Removing the loop-thread hop may reduce latency
while also removing cross-core pipelining between HTTP framing and Python GIL
work. Include `/t-timer` when comparing: positive-delay timers use the
process-wide high-resolution Rust timer dispatcher rather than Tokio's ~1ms
timer wheel.

Attribution tools: `py-spy record --gil` (Task machinery shows up as
`ensure_future`), `strace -f -c -p <pid>` (cross-thread wakeup writes and
`epoll_pwait`s per request). Disable access logging first — the Python logging
stack is large enough (~35-40% of GIL samples) to drown the signal.

## Macro HTTP benchmarks

```bash
# needs bombardier: go install github.com/codesenberg/bombardier@latest
just save-bench          # full suite → bench/BENCHMARK.md; git diff shows the delta
```

For low-noise runs: pin the server (`taskset -c 0-7 python manage.py runbolt …`)
and the load generator to disjoint cores, disable turbo if comparing small
deltas, and take 3+ repetitions — a 2% gate on a single noisy run is
meaningless.

## CPU flamegraphs (Rust + native)

The `profiling` cargo profile (release + debug symbols) exists for this.

```bash
cargo install flamegraph
just build-profiling                       # maturin develop --profile profiling
# terminal 1: run the server under perf
flamegraph -o flame.svg -- python python/example/manage.py runbolt --port 8001
# terminal 2: apply load
bombardier -c 100 -n 200000 http://127.0.0.1:8001/
```

Or attach to a running worker: `perf record -F 997 -g -p <pid> -- sleep 15 &&
perf script | inferno-collapse-perf | inferno-flamegraph > flame.svg`.

For "we moved X to Rust / removed allocation Y" claims, capture a before/after
pair and diff visually — the disappearing frames are the evidence.

## Python-side profiling (py-spy)

```bash
uv tool install py-spy
py-spy record --native -o pyflame.svg --pid <worker-pid> --duration 15
py-spy dump --pid <worker-pid>          # stack snapshot
```

`--native` shows the Rust frames interleaved with Python frames — useful for
seeing GIL-block composition. `py-spy dump` repeatedly during load approximates
GIL hold distribution.

## Allocation counting

For allocation-elimination claims, count allocations per fixed request batch:

```bash
# jemalloc stats (build with the jemalloc feature):
maturin develop --release -- --features jemalloc
# or heaptrack on a single-process server:
heaptrack python python/example/manage.py runbolt --port 8001 --processes 1
bombardier -c 10 -n 10000 http://127.0.0.1:8001/ && kill %1
heaptrack_print heaptrack.*.gz | head -50
```

Report allocations/request before vs after — this proves the mechanism even
when the RPS delta drowns in noise.

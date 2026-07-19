# Profiling & Benchmarking Guide

Django-Bolt has a layered measurement strategy. Different changes are visible at
different altitudes — use the right one for the change you're making:

| Layer | Tool | Proves | Command |
|---|---|---|---|
| Rust micro | criterion | Pure-Rust hot functions (query/cookie parsing, coercion) | `just bench-rust` |
| Python micro | pytest-benchmark | Injectors, dependency extraction, serialization | `just bench-micro` |
| In-process full stack | `TestClient` benches | Whole Rust+Python pipeline, no sockets | (in `python/benchmarks/`) |
| Macro HTTP | bombardier | End-to-end RPS + latency distribution | `just save-bench` |
| Regression gate | `benchmark_compare.py` | RPS **and** p99 deltas vs baseline | `just bench-gate` |

Suggested proof discipline per optimization PR: micro-bench shows the isolated
win → in-process bench shows per-request µs → `just save-bench` + `just
bench-gate` shows it survives end-to-end → a before/after flamegraph pair as
the narrative artifact.

## Dispatch-path probes (async bridge cost)

`python/tests/integration/apps/dispatch_probes.py` has one route per dispatch
mechanism (`/t-sync`, `/t-trivial`, `/t-ready`, `/t-sleep0`, `/t-thread`, …).
Point a `runbolt` server at it (`BOLT_API = ["tests.integration.apps.dispatch_probes:api"]`)
and compare per-request latency over a keepalive connection:

- `t_ready − t_trivial` = pure async-bridge cost (no real suspension)
- `t_sleep0 − t_ready` = one suspend/resume cycle
- rerun with `DJANGO_BOLT_EAGER_DISPATCH=0` for eager-vs-legacy bridge deltas
- rerun with `DJANGO_BOLT_WORKER_LOOP=0` for worker-local-vs-loop-thread deltas

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

## Rust micro-benchmarks (criterion)

```bash
just bench-rust               # cargo bench — results in target/criterion/
cargo bench -- parse_query    # filter to one group
```

Criterion keeps its own baselines under `target/criterion/` and prints the
delta vs the previous run — run once before your change, once after.

Note: `pyo3/extension-module` is enabled by maturin (pyproject.toml), not in
Cargo.toml, precisely so `cargo bench`/`cargo test` can link libpython. If the
build can't find Python ≥3.12, set `PYO3_PYTHON=/path/to/python3.12`.

## Python micro-benchmarks (pytest-benchmark)

```bash
just bench-micro
# Compare before/after:
just bench-micro-save NAME=before   # saves .benchmarks/<...>_before.json
just bench-micro-save NAME=after
uv run --with pytest --with pytest-benchmark pytest-benchmark compare
```

These live in `python/benchmarks/` (NOT collected by `just test-py`) and drive
the compiled injectors, dependency argument extraction, and
`serialize_response_sync` with synthetic request dicts.

## Macro HTTP benchmarks + regression gate

```bash
# needs bombardier: go install github.com/codesenberg/bombardier@latest
just save-bench          # baseline → dev → rotate (see CLAUDE.md)
just bench-gate          # deterministic pass/fail: per-endpoint RPS AND p99
```

The gate fails on: any endpoint losing >2% RPS, any endpoint's p99 latency
growing >25%, or the core-endpoint median gain missing its target. Tune with
`--max-regression`, `--max-p99-regression`, `--core-median-min-gain`.

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

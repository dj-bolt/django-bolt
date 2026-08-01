# micro-reflex Benchmarks — vs Reflex, and the WorkerLoop A/B

Measured 2026-08-01 in a 4-core Linux container, Python 3.12, single-process
server, single Python client process, localhost TCP. Reflex 0.9.7 backend-only
(its production event path; no browser, no Next.js in the loop for either side).
All event round-trips are the same workload: a counter `increment` handler,
response = state delta (Reflex) / slot patches (micro-reflex). Latency is
send→receive per event; throughput timer starts after all connections are
connected and warmed (first event per connection excluded).

Numbers are medians of repeated runs; run-to-run spread was within ~±7%.

## 1. micro-reflex (django-bolt HEAD) vs Reflex 0.9.7

Reflex was measured under both uvicorn and granian; uvicorn was equal or
better in every cell, so the uvicorn numbers are shown (granian was up to 65%
worse at p99, e.g. 70.5ms vs 42.6ms at 32 conns).

| conns | metric | micro-reflex | Reflex (uvicorn) | ratio |
|---|---|---|---|---|
| 1 | throughput | ~870/s | ~854/s | ~1.0x |
| 1 | p50 / p99 | 1.10ms / 1.9ms | 1.06ms / 1.6ms | ~1.0x |
| 8 | throughput | ~1,750/s | 1,357/s | **1.3x** |
| 8 | p50 / p99 | 4.3ms / 8.5ms | 5.5ms / 8.4ms | 1.3x on p50 |
| 32 | throughput | ~1,900/s | 1,251/s | **1.5x** |
| 32 | p50 / p99 | 15.9ms / 33ms | 22.3ms / 42.6ms | 1.4x / 1.3x |

Reading:

- **Single connection is transport-bound, not framework-bound.** Both stacks
  land at ~1ms RTT because the cost is TCP + event-loop wakeups on both ends
  of a trivial handler; the in-process figure for the same micro-reflex
  dispatch is ~90µs (see `python/tests/test_microreflex.py` throughput smoke),
  so ~0.9ms of the TCP figure is transport and client, not dispatch.
- **Under concurrency micro-reflex pulls ahead 1.3–1.5x** with tighter tails —
  routing, WebSocket framing, and connection handling are in Rust and off the
  GIL, so Python time is spent only on the handler + slot diff.
- Both sides degrade with concurrency because per-event Python work
  serializes on the GIL; the single Python *client* process is also a
  non-trivial share of CPU at 32 conns (it bounds absolute numbers, not the
  comparison — both sides used the identical client).
- The counter state is tiny, which flatters Reflex: its per-event pipeline
  (full state tree, delta serialization, socket.io envelope) grows with state
  size, while micro-reflex work grows only with the page's dynamic slots.

**Page load is not comparable head-to-head** (Reflex backend-only serves no
page; full Reflex serves a Next.js bundle + hydration + a hydrate event).
micro-reflex serves the entire interactive page as one pre-rendered HTML
string through the Rust sync-dispatch fast path: p50 589µs, p99 910µs at 1
conn; ~2,600 pages/s at 32 conns with p99 14.9ms — and that ceiling is the
benchmark client, not the server.

## 2. Did the WorkerLoop help?

Three builds, same benchmarks:

- **v0.9.1** (`5683ab1`) — before the WorkerLoop existed
- **pre-#273** (`4612b46`) — WorkerLoop for async HTTP dispatch (#268), WebSocket still on the plain asyncio loop
- **HEAD** — WebSocket + streaming also on the WorkerLoop (#273)

### Async HTTP dispatch (`await asyncio.sleep(0)` handler) — the WorkerLoop's design target

| conns | v0.9.1 | HEAD (WorkerLoop) | delta |
|---|---|---|---|
| 1 | 1,225/s · p50 791µs | 1,497/s · p50 652µs | **+22% rps, −18% p50** |
| 8 | 2,270/s · p99 5.4ms | 2,920/s · p99 3.8ms | **+29% rps, −30% p99** |
| 32 | 2,329/s · p99 20.1ms | 3,105/s · p99 14.0ms | **+33% rps, −30% p99** |

pre-#273 measured within a few percent of HEAD on this path (the HTTP-side
WorkerLoop is present in both). **Yes — the WorkerLoop clearly helped where
it was aimed**: it removed the per-request Task machinery and cross-thread
wakeups that the dispatch probes had shown to be ~50% of GIL time.

### WebSocket event dispatch (micro-reflex's transport)

| conns | WS on asyncio loop (v0.9.1 / pre-#273) | HEAD (WS on WorkerLoop) | delta |
|---|---|---|---|
| 1 | ~950–980/s | ~830–900/s | ~−6% |
| 8 | ~2,000–2,085/s | ~1,675–1,845/s | ~−10% |
| 32 | ~2,130–2,180/s | ~1,830–1,965/s | ~−11% |

Moving WebSocket onto the WorkerLoop (#273) costs ~6–11% on a pure WS
echo-style workload. That change was made for correctness, not speed: a
future/queue/lock shared between a WebSocket handler and an HTTP handler must
live on one loop, or cross-loop `set_result` never wakes the foreign selector
and the WebSocket side hangs (see the comment in
`src/websocket/handler.rs`). The loss matches the known trade-off documented
in CLAUDE.md: putting Python work on the HTTP worker removes wakeups but also
removes cross-core pipelining between the transport thread and the loop
thread — which is exactly what a ping-pong benchmark maximizes. Even paying
it, micro-reflex holds a 1.3–1.5x lead over Reflex.

## Reproducing

```bash
# micro-reflex event round-trips (run from a repo checkout; needs `websockets`)
PYTHONPATH=python .venv/bin/python bench/microreflex/bench_event_roundtrip.py 32 200 label

# async HTTP dispatch probe
PYTHONPATH=python .venv/bin/python bench/microreflex/bench_http_async.py 8 1200 label
# (page GET variant: BENCH_APP=microreflex_demo BENCH_PATH=/ ...)

# Reflex: pip install reflex python-socketio aiohttp; create the counter app from
# bench_reflex.py's docstring/imports; uvicorn counter.counter:app --port 8099;
# then: python bench_reflex.py 8099 32 200
```

Older builds are benchmarked by checking out the commit in a git worktree,
running `maturin develop` there, copying `python/django_bolt/microreflex/` +
the demo app in (both pure Python), and pointing the same scripts at it.

Caveats: single client process (client CPU bounds absolute throughput at 32
conns), 4 vCPUs shared by client and server, one server process. Treat ratios
as the signal, absolute numbers as environment-specific.

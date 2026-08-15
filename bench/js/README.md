# Django-Bolt vs Hono vs Elysia — JSON payload benchmark

Minimal [Hono](https://hono.dev) and [Elysia](https://elysiajs.com) apps that
mirror the Django-Bolt example project, so all three can be compared on the same
payloads, on the same machine, with the same load generator.

## What is compared

| Route | Payload | Serialized size |
| --- | --- | --- |
| `/` | `{"message":"Hello Django"}` | 26 B (routing-overhead control) |
| `/1k-json` | `python/example/test_data/1K.json` | 1171 B |
| `/10k-json` | `python/example/test_data/10K.json` | 10864 B |

Every stack reads the **same JSON files** from `python/example/test_data/` at
startup and serializes them **per request** — msgspec `encode` for Bolt,
`JSON.stringify` via `c.json` for Hono, and Elysia's plain-object return.
Nothing is pre-encoded once and replayed, so the measurement includes each
framework's real serialization cost. Response bodies are byte-identical across
all four runtimes, which `compare.sh` re-checks on every run (the `bytes`
column). Each framework is started the way its own docs recommend: Bun's
`Bun.serve({ fetch: app.fetch })` for Hono, Elysia's `.listen()`.

## Running it

```bash
# Everything: bolt, hono on node, hono on bun, elysia on bun
just bench-js

# Same, with explicit knobs: host port c n processes
just bench-js 127.0.0.1 8011 200 200000 8

# A subset of runtimes
RUNTIMES="bolt elysia-bun" ./bench/js/compare.sh

# One endpoint only
ENDPOINTS="/1k-json" ./bench/js/compare.sh
```

Requires `bombardier`, `lsof`, `node`, and `uv`. `bun` is optional — the
`hono-bun` and `elysia-bun` runtimes are skipped with a warning if it is absent.

Every runtime binds the **same port** in sequence, one at a time, and each gets
a warmup run (`WARMUP_N`, default 5000) before the measured run. The script
aborts if anything already holds the port on startup, or if a previous runtime
has not released it — otherwise a foreign server would answer the readiness
probe and get benchmarked in place of the runtime under test.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8001` | Bind address, shared by all runtimes |
| `C` | `100` | Concurrent connections |
| `N` | `100000` | Requests per endpoint |
| `PROCESSES` | `1` | Server processes (see below) |
| `WORKERS` | `1` | `DJANGO_BOLT_WORKERS`, Bolt only |
| `WARMUP_N` | `5000` | Unmeasured warmup requests |
| `RUNTIMES` | `bolt hono-node hono-bun elysia-bun` | Which servers to run |
| `ENDPOINTS` | `/1k-json /10k-json` | Which routes to hit |

`PROCESSES` maps to the closest equivalent on each stack: `runbolt --processes N`
for Bolt, `node:cluster` with `SCHED_NONE` for Hono/node, and N separate `bun`
processes sharing the port via `reusePort` for both Bun runtimes (Bun has no
cluster module). All of them end up load-balanced by the kernel over
`SO_REUSEPORT`.

## Running the JS apps on their own

```bash
cd bench/js
bun install                          # or: npm install (node runtime only)
PORT=8002 npm run start:hono-node
PORT=8003 npm run start:hono-bun
PORT=8004 npm run start:elysia-bun
```

## Measured results

Loopback, one 12-core machine, `C=100`, `N=100000` per endpoint, `WORKERS=1`.
Absolute numbers are hardware-specific; the ratios are the point. All rows below
come from one machine state, so they are comparable to each other — but not to
numbers taken on a different day, because unrelated background load moves the
absolute figures by 10–15%.

### Single process (`PROCESSES=1`)

One process and one worker thread per runtime — 11 cores idle on all four.

| Endpoint | Runtime | Reqs/sec | p99 | Throughput | vs bolt |
| --- | --- | --- | --- | --- | --- |
| `/` | elysia / bun | **88379** | 2.48 ms | 17.6 MB/s | 1.21× |
| `/` | django-bolt | 72766 | 2.96 ms | 13.6 MB/s | — |
| `/` | hono / bun | 67714 | 3.12 ms | 12.6 MB/s | 0.93× |
| `/` | hono / node | 35263 | 5.26 ms | 8.2 MB/s | 0.48× |
| `/1k-json` | elysia / bun | **75625** | 2.75 ms | 97.9 MB/s | 1.23× |
| `/1k-json` | django-bolt | 61396 | 3.53 ms | 79.0 MB/s | — |
| `/1k-json` | hono / bun | 58137 | 3.91 ms | 74.8 MB/s | 0.95× |
| `/1k-json` | hono / node | 29729 | 5.71 ms | 39.6 MB/s | 0.48× |
| `/10k-json` | django-bolt | **35987** | 4.92 ms | 378.8 MB/s | — |
| `/10k-json` | elysia / bun | 30673 | 5.74 ms | 323.5 MB/s | 0.85× |
| `/10k-json` | hono / bun | 24981 | 7.09 ms | 263.0 MB/s | 0.69× |
| `/10k-json` | hono / node | 17977 | 9.08 ms | 190.1 MB/s | 0.50× |

A second pass with the runtime order reversed
(`RUNTIMES="elysia-bun hono-bun hono-node bolt"`) reproduced every figure within
2.6%, so the Elysia win at 1 KB and the Bolt win at 10 KB are not ordering or
drift artifacts.

### 8 processes (`PROCESSES=8`)

| Endpoint | Runtime | Reqs/sec | p99 | Throughput | vs bolt |
| --- | --- | --- | --- | --- | --- |
| `/1k-json` | elysia / bun | **264037** | 3.03 ms | 341.2 MB/s | 1.05× |
| `/1k-json` | django-bolt | 251203 | 2.97 ms | 322.5 MB/s | — |
| `/1k-json` | hono / bun | 209760 | 3.53 ms | 269.4 MB/s | 0.84× |
| `/1k-json` | hono / node | 96639 | 4.66 ms | 128.1 MB/s | 0.38× |
| `/10k-json` | django-bolt | **157462** | 3.31 ms | 1.62 GB/s | — |
| `/10k-json` | elysia / bun | 123535 | 3.99 ms | 1.27 GB/s | 0.78× |
| `/10k-json` | hono / bun | 110633 | 3.68 ms | 1.14 GB/s | 0.70× |
| `/10k-json` | hono / node | 79110 | 4.05 ms | 833.8 MB/s | 0.50× |

Caveat for the 8-process rows: bombardier shares the same 12 cores as the 8
server processes, so the load generator is part of the system under test at
these rates. Read those rows as a floor, not a ceiling. For a clean number,
drive the load from a second machine.

## Reading the result

Zero non-2xx responses in every run. Run-to-run spread on this box is roughly
±5%, so treat gaps under ~10% as a tie.

- **Elysia is the fastest small-payload server here.** It leads Bolt by 1.2× on
  26 B and 1 KB at one process. At 8 processes the 1 KB lead shrinks to 1.05× —
  a tie.
- **Bolt wins as the payload grows**, and the margin holds when scaled out:
  1.17× over Elysia on 10 KB at one process, 1.27× at eight. Serialization and
  body handling dominate at that size, which is where msgspec plus the zero-copy
  response path pays off.
- **The crossover is between 1 KB and 10 KB.** Below it, per-request routing and
  dispatch overhead decide the winner and Elysia's is lower. Above it, encode
  throughput decides and Bolt's is higher.
- **Hono on Bun trails Elysia on the same runtime** by 1.2–1.3×, so these
  numbers separate framework overhead from runtime overhead: the Bun-vs-Node gap
  is about 2×, and the Elysia-vs-Hono gap is real but smaller.

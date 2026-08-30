---
icon: lucide/gauge
description: Django-Bolt benchmarks — 300k+ requests/second measured with bombardier, with exact conditions, per-endpoint results (JSON, ORM, auth, forms, static files, SSE), comparison against Bun/Node JavaScript frameworks and Python frameworks, and reproduction commands.
---

# Benchmarks

**Django-Bolt is the fastest Python web framework we have measured**: ~311,000 requests/second on a JSON hello-world from a single 12-core desktop, ~187,000 req/s for a 10 KB JSON body, and 21,000–27,000 req/s for a 10-row Django ORM query. Every number below states its conditions and can be reproduced with one command.

!!! note "Read this first"
    Absolute numbers are hardware-specific. **Publish the conditions with the number** — the project holds itself to that rule, and you should too when you quote these figures. Run-to-run spread on the reference machine is about ±5%; treat gaps under ~10% as a tie.

## Reference setup

| | |
| --- | --- |
| Machine | AMD Ryzen 5 5600G (6 cores / 12 threads), 16 GB RAM, Linux |
| Load generator | [bombardier](https://github.com/codesenberg/bombardier), loopback, on the same machine |
| Concurrency | `C=100` connections, `N=100000` requests per endpoint |
| Server | `python manage.py runbolt --processes 8` (8 processes × 1 Actix worker, `SO_REUSEPORT`) |
| App | [`python/example`](https://github.com/dj-bolt/django-bolt/tree/master/python/example) — full Django project with admin, sessions, CSRF, messages, CSP middleware installed |
| Django-Bolt | 0.10.0 · results file: [`python/benchmark/BENCHMARK.md`](https://github.com/dj-bolt/django-bolt/blob/master/python/benchmark/BENCHMARK.md) (2026-07-28) |

Because bombardier shares the 12 cores with the 8 server processes, the load generator is part of the system under test. Read the numbers as a **floor**, not a ceiling.

## Results by endpoint

| Endpoint | What it exercises | Req/s | p50 | p99 |
| --- | --- | ---: | ---: | ---: |
| `GET /` | JSON hello-world, routing overhead | **311,270** | 234 µs | 2.20 ms |
| `GET /html` | HTML response | 285,927 | — | — |
| `GET /items/1?q=hello` | Path + query params, typed | 264,152 | — | — |
| `PUT /items/1` | JSON body → `msgspec.Struct` | 256,945 | — | — |
| `GET /header`, `/cookie` | Header / cookie extraction | ~255,000 | — | — |
| `POST /bench/parse` | JSON parse + validate (Django Ninja-style) | 251,426 | — | — |
| `GET /exc` | Raised `HTTPException` → error response | 247,087 | — | — |
| `POST /form` | URL-encoded form | 217,731 | — | — |
| `GET /cbv-simple` | Class-based `APIView` | 219,367 | — | — |
| `GET /sync-10k-json` | 10 KB JSON response (sync handler) | 187,186 | 473 µs | 2.17 ms |
| `GET /10k-json` | 10 KB JSON response (async handler) | 184,059 | 475 µs | 1.78 ms |
| `POST /upload` | Multipart file upload | 178,364 | — | — |
| `GET /auth/context` | JWT validated in Rust, no DB | 158,060 | — | — |
| `GET /static/…/asset_1k.css` | Static file, 1 KB | 159,114 | — | — |
| `GET /static/…/asset_100k.js` | Static file, 100 KB | 94,226 | — | — |
| `GET /bench/list` | List of 100 structs | 89,229 | — | — |
| `GET /file-static` | `FileResponse` | 47,177 | — | — |
| `GET /auth/me` | JWT + load `request.user` from DB | 40,312 | — | — |
| `GET /users/mini10` | ORM: 10 rows, 2 fields, SQLite, async | 26,694 | — | — |
| `GET /users/full10` | ORM: 10 full rows, SQLite, async | 20,963 | — | — |
| `GET /users/sync-full10` | ORM: 10 full rows, SQLite, sync handler | 15,767 | — | — |
| `GET /middleware/demo` | Full Django middleware stack + messages framework | 8,744 | — | — |

The ORM rows are bounded by SQLite's single-writer file lock and by the ORM's own Python cost, not by Bolt; on PostgreSQL the ORM executor uses more threads. The middleware row runs the *entire* `settings.MIDDLEWARE` chain (sessions, CSRF, auth, messages, CSP) in Python for every request — it is there to show that nothing is stripped, and what the full stack costs.

### Streaming: Server-Sent Events

10,000 concurrent SSE clients held open for 60 seconds:

| Metric | Value |
| --- | --- |
| Successful connections | 10,000 / 10,000 (100%) |
| Aggregate throughput | 9,489 messages/s |
| Messages per client | 57.3 average |
| CPU | 11.9% average (peak 101.9% of one core) |
| Memory | 236 MB RSS |

## Against JavaScript runtimes

Same JSON payloads, same machine, same load generator, byte-identical response bodies checked on every run. `C=100`, `N=100000`.

**8 processes each** (Bolt: `--processes 8`; Hono/Node: `node:cluster`; Bun: 8 processes with `reusePort`):

| Payload | Django-Bolt | Elysia / Bun | Hono / Bun | Hono / Node |
| --- | ---: | ---: | ---: | ---: |
| 1 KB JSON | 251,203 | **264,037** | 209,760 | 96,639 |
| 10 KB JSON | **157,462** | 123,535 | 110,633 | 79,110 |

**Single process:**

| Payload | Django-Bolt | Elysia / Bun | Hono / Bun | Hono / Node |
| --- | ---: | ---: | ---: | ---: |
| 26 B JSON (`/`) | 72,766 | **88,379** | 67,714 | 35,263 |
| 1 KB JSON | 61,396 | **75,625** | 58,137 | 29,729 |
| 10 KB JSON | **35,987** | 30,673 | 24,981 | 17,977 |

Reading: Elysia leads on tiny payloads at one process (1.2×) and ties Bolt at eight (1.05×, inside the noise). Bolt leads on 10 KB at both scales (1.17× / 1.27×) — once serialization and body handling dominate, msgspec plus the zero-copy response path win. A Python framework beating Bun-native servers on realistic payload sizes is the headline here.

## Against Python frameworks

A separate harness, [python-api-frameworks-benchmark](https://github.com/FarhanAliRaza/python-api-frameworks-benchmark),
runs Django-Bolt, Litestar, and FastAPI against the same seven endpoints and the same PostgreSQL data (2026-08-30, same machine, Python 3.14, one process each).
Each framework runs alone in a Docker container with a 750 MB memory limit and no CPU limit.
bombardier runs on the host with `C=100` for 10 s per endpoint. Each framework starts twice;
the table shows the median.

| Endpoint | What it exercises | Django-Bolt | Litestar + uvicorn | FastAPI + uvicorn |
| --- | --- | ---: | ---: | ---: |
| `/json-1k` | 1 KB JSON response | **43,541** | 15,477 | 7,517 |
| `/json-10k` | 10 KB JSON response | **27,610** | 12,925 | 1,805 |
| `/db` | 10 rows from PostgreSQL | **2,881** | 1,321 | 1,237 |
| `/articles` | 20 articles + author + tags, paginated | **301** | 204 | 197 |
| `/articles/1` | 1 article + author + tags + comments | **561** | 420 | 406 |
| `/auth/me` | JWT cookie auth, load the user | **4,478** | 1,155 | 941 |
| `/auth/articles` | JWT cookie auth + the paginated query | **285** | 173 | 175 |

Django-Bolt uses the Django ORM with psycopg 3 on a bounded thread pool. Litestar and FastAPI
use SQLAlchemy with asyncpg. All three use the same pool size (8). Litestar and Django-Bolt
serialize with msgspec; FastAPI serializes with Pydantic. The ORM rows compare two database
stacks as well as two frameworks. Read them with that in mind.

## Reproduce

```bash
git clone https://github.com/dj-bolt/django-bolt.git && cd django-bolt
uv sync && just build
go install github.com/codesenberg/bombardier@latest

just save-bench                    # full suite → python/benchmark/BENCHMARK.md
just save-bench 127.0.0.1 8001 100 100000 8   # host port C N processes
```

Cross-framework table (Docker, PostgreSQL, one process per framework):

```bash
git clone https://github.com/FarhanAliRaza/python-api-frameworks-benchmark.git && cd python-api-frameworks-benchmark
./run_all.sh --docker --frameworks bolt litestar-uvicorn fastapi-uvicorn --restarts 2
```

The app code for each framework is in the repository root: `django_project/api.py` (Bolt),
`litestar_app.py`, and `fastapi_app.py`.

## How to quote these numbers

- ✅ "Django-Bolt: ~311k req/s JSON hello-world, 8 processes, C=100, Ryzen 5 5600G, loopback bombardier."
- ✅ "Django-Bolt beats Bun/Elysia by 1.27× on 10 KB JSON at 8 processes (same machine)."
- ❌ "Django-Bolt does 300k RPS" with no conditions.
- ❌ Comparing to numbers measured on a different machine or day.

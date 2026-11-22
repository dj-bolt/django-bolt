---
description: Django-Bolt throughput and latency for each kind of route, against Litestar, FastAPI and JavaScript servers, with the conditions and the commands to reproduce.
---

# Benchmarks

This page gives the measured throughput of Django-Bolt, the conditions of each measurement, and the commands to run them yourself.

A benchmark number is true only for its conditions.
When you quote a number from this page, quote its conditions too.

## Summary

| Test | Result | Conditions |
| --- | ---: | --- |
| JSON route, hello world | 324,493 req/s | 8 processes, 100 connections |
| 10 KB JSON response, sync handler | 192,587 req/s | 8 processes, 100 connections |
| Django ORM, 10 rows, async handler | 19,970 req/s | 8 processes, 100 connections, SQLite |
| 1 KB JSON against Litestar and FastAPI | 2.8× Litestar, 5.8× FastAPI | 1 process each, 100 connections |
| 10 rows from PostgreSQL against Litestar and FastAPI | 2.2× Litestar, 2.3× FastAPI | 1 process each, 100 connections |

## Bolt alone

### Setup

| | |
| --- | --- |
| Machine | AMD Ryzen 5 5600G (6 cores, 12 threads), 16 GB RAM, Linux |
| Load generator | [bombardier](https://github.com/codesenberg/bombardier) on the same machine, over loopback |
| Load | 100 connections, 100,000 requests for each route |
| Server | `runbolt --processes 8`, 1 worker thread for each process |
| Application | [`python/example`](https://github.com/dj-bolt/django-bolt/tree/master/python/example), a Django project with the admin, sessions, CSRF, messages and CSP installed |
| Date | 2026-09-19 |
| Raw results | [`python/benchmark/BENCHMARK.md`](https://github.com/dj-bolt/django-bolt/blob/master/python/benchmark/BENCHMARK.md) |

bombardier uses the same 12 CPU threads as the 8 server processes.
Thus the load generator takes CPU time from the server. A server on its own machine can do more.
The spread from run to run is about 5%. Treat a difference of less than 10% as equal.

### Results

| Route | What the route does | Req/s | p50 | p99 |
| --- | --- | ---: | ---: | ---: |
| `GET /` | Returns a small JSON object | 324,493 | 0.20 ms | 2.71 ms |
| `GET /html` | Returns HTML | 303,162 | 0.25 ms | 2.19 ms |
| `GET /items/1?q=hello` | Reads a path and a query parameter | 285,130 | 0.27 ms | 2.85 ms |
| `GET /header` | Reads a header | 264,436 | 0.29 ms | 2.06 ms |
| `GET /cookie` | Reads a cookie | 260,545 | 0.30 ms | 2.29 ms |
| `PUT /items/1` | Decodes a JSON body to a `msgspec.Struct` | 259,226 | 0.31 ms | 2.24 ms |
| `GET /exc` | Raises `HTTPException` | 239,760 | 0.33 ms | 3.01 ms |
| `POST /bench/parse` | Decodes and validates a JSON body | 233,472 | 0.30 ms | 3.06 ms |
| `POST /form` | Reads a URL-encoded form | 219,260 | 0.33 ms | 2.72 ms |
| `GET /cbv-items/1` | Class-based view, retrieve | 215,107 | 0.39 ms | 2.03 ms |
| `GET /cbv-simple` | Class-based view, `APIView` | 209,883 | 0.38 ms | 2.77 ms |
| `GET /sync-10k-json` | 10 KB JSON, `def` handler | 192,587 | 0.42 ms | 2.18 ms |
| `POST /upload` | Multipart file upload | 185,406 | 0.44 ms | 2.77 ms |
| `GET /10k-json` | 10 KB JSON, `async def` handler | 164,709 | 0.51 ms | 3.21 ms |
| `GET /auth/context` | Checks a JWT in Rust, no database | 155,104 | 0.57 ms | 2.10 ms |
| `GET /static/…/asset_1k.css` | Static file, 1 KB | 150,301 | 0.54 ms | 3.47 ms |
| `GET /feed` | List of 100 items of a union type | 86,837 | 1.04 ms | 3.01 ms |
| `GET /static/…/asset_100k.js` | Static file, 100 KB | 82,468 | 1.11 ms | 4.00 ms |
| `GET /file-static` | `FileResponse` | 48,691 | 1.80 ms | 6.14 ms |
| `GET /auth/me` | Checks a JWT, then loads `request.user` from the database | 38,908 | 2.73 ms | 6.24 ms |
| `GET /users/mini10` | ORM, 10 rows, 2 fields, `async def` | 25,906 | 3.60 ms | 8.55 ms |
| `GET /users/full10` | ORM, 10 full rows, `async def` | 19,970 | 4.53 ms | 11.89 ms |
| `GET /users/sync-full10` | ORM, 10 full rows, `def` | 16,346 | 5.18 ms | 19.10 ms |
| `GET /middleware/demo` | All of `settings.MIDDLEWARE`, and the messages framework | 5,996 | 16.51 ms | 50.96 ms |

How to read the results:

- The ORM routes use SQLite. The cost of the ORM in Python and the SQLite file lock set their limit, not Bolt.
- The `/middleware/demo` route runs the full Django middleware stack in Python for each request. It shows the cost of that stack. Bolt routes do not run Django middleware unless you turn it on.
- The rows with `p50` and `p99` give the latency at the 50th and 99th percentile.

## Against Litestar and FastAPI

A separate harness, [python-api-frameworks-benchmark](https://github.com/FarhanAliRaza/python-api-frameworks-benchmark),
runs the same seven routes on Django-Bolt, Litestar and FastAPI, with the same PostgreSQL data.

| | |
| --- | --- |
| Machine | The machine above |
| Python | 3.14 |
| Containers | Each framework runs alone in Docker, with a 750 MB memory limit and no CPU limit |
| Server | 1 process for each framework. Litestar and FastAPI run on uvicorn. |
| Load | bombardier on the host, 100 connections, 10 seconds for each route |
| Runs | Each framework starts two times. The table gives the median. |
| Date | 2026-08-30 |

| Route | What the route does | Django-Bolt | Litestar | FastAPI |
| --- | --- | ---: | ---: | ---: |
| `/json-1k` | 1 KB JSON response | 43,541 | 15,477 | 7,517 |
| `/json-10k` | 10 KB JSON response | 27,610 | 12,925 | 1,805 |
| `/db` | 10 rows from PostgreSQL | 2,881 | 1,321 | 1,237 |
| `/articles` | 20 articles with author and tags, one page | 301 | 204 | 197 |
| `/articles/1` | 1 article with author, tags and comments | 561 | 420 | 406 |
| `/auth/me` | JWT cookie auth, then load the user | 4,478 | 1,155 | 941 |
| `/auth/articles` | JWT cookie auth, then the page of articles | 285 | 173 | 175 |

Values are requests per second. Higher is better.

The database routes compare two database stacks as well as three frameworks:

- Django-Bolt uses the Django ORM with psycopg 3 on a bounded thread pool.
- Litestar and FastAPI use SQLAlchemy with asyncpg.
- All three use a pool of 8 connections.
- Django-Bolt and Litestar encode with msgspec. FastAPI encodes with Pydantic.

## Against JavaScript servers

These results are older. They use Django-Bolt 0.10 (July 2026) on the machine above.
Each server returns the same JSON bytes. 100 connections, 100,000 requests.

With 8 processes for each server (Bolt `--processes 8`, Node `node:cluster`, Bun 8 processes with `reusePort`):

| Payload | Django-Bolt | Elysia on Bun | Hono on Bun | Hono on Node |
| --- | ---: | ---: | ---: | ---: |
| 1 KB JSON | 251,203 | 264,037 | 209,760 | 96,639 |
| 10 KB JSON | 157,462 | 123,535 | 110,633 | 79,110 |

With 1 process for each server:

| Payload | Django-Bolt | Elysia on Bun | Hono on Bun | Hono on Node |
| --- | ---: | ---: | ---: | ---: |
| 26 B JSON | 72,766 | 88,379 | 67,714 | 35,263 |
| 1 KB JSON | 61,396 | 75,625 | 58,137 | 29,729 |
| 10 KB JSON | 35,987 | 30,673 | 24,981 | 17,977 |

Elysia is faster for small bodies: 1.2× with 1 process. With 8 processes the two are equal within the noise.
Bolt is faster for 10 KB bodies: 1.17× with 1 process and 1.27× with 8 processes.
For a large body, the cost of encoding sets the result, and msgspec is fast at encoding.

## Reproduce the results

### Bolt alone

You need [uv](https://docs.astral.sh/uv/), a Rust toolchain, [just](https://github.com/casey/just) and Go (for bombardier).

```bash
git clone https://github.com/dj-bolt/django-bolt.git
cd django-bolt
uv sync
just build
go install github.com/codesenberg/bombardier@latest
just save-bench
```

`just save-bench` writes `python/benchmark/BENCHMARK.md`.
To change the load, give the host, the port, the connections, the requests and the processes:

```bash
just save-bench 127.0.0.1 8001 100 100000 8
```

### Against Litestar and FastAPI

You need Docker.

```bash
git clone https://github.com/FarhanAliRaza/python-api-frameworks-benchmark.git
cd python-api-frameworks-benchmark
./run_all.sh --docker --frameworks bolt litestar-uvicorn fastapi-uvicorn --restarts 2
```

The code for each framework is in the root of that repository: `django_project/api.py` (Bolt), `litestar_app.py` and `fastapi_app.py`.

## Quote a result

Give the number, the route, the processes, the connections and the machine. For example:

> Django-Bolt: 324,493 req/s on a JSON route. 8 processes, 100 connections, Ryzen 5 5600G, bombardier over loopback, 2026-09-19.

Do not compare a number from this page with a number from a different machine or a different day.

## See also

- [How it works](architecture.md): why Bolt is fast.
- [Deployment](topics/deployment.md): choose the number of processes and worker threads.
- [Comparison](comparison.md): features and trade-offs next to other frameworks.

---
icon: lucide/layers
description: How Django-Bolt works — a Rust (Actix Web + Tokio) HTTP server calls Python handlers through PyO3, with routing, auth, guards, CORS, rate limiting and compression running in Rust without the GIL. No WSGI, no ASGI server.
---

# How Django-Bolt works

Django-Bolt is **not** a WSGI or ASGI application and does not run behind gunicorn or uvicorn. It is a Rust HTTP server that embeds the Python interpreter and calls your Django handlers directly. This page explains the request path and why it is fast.

## The request path

```
HTTP request
   │
   ▼
Actix Web (Rust, Tokio runtime)
   │  HTTP/1.1 parsing, keep-alive, TLS termination upstream
   ▼
Router (matchit — zero-copy path matching)
   │
   ▼
Rust middleware pipeline — no Python, no GIL
   │  CORS · rate limiting (token bucket) · compression (gzip/brotli/zstd)
   ▼
Authentication (Rust)
   │  JWT signature + expiry · API-key lookup
   ▼
Guards (Rust)
   │  IsAuthenticated · Requires(claim, ...) · AllowAny
   ▼
Dispatch decision
   ├─ sync fast path: handler needs no event loop → one GIL block, response built in Rust
   └─ async path: handler awaits → scheduled on a persistent per-process worker loop
   │
   ▼
Your Python handler
   │  typed params (path/query/header/cookie/form/body) · Depends(...) · Django ORM
   ▼
msgspec serialization → zero-copy bytes → Actix response
```

Only the last two boxes touch Python. Everything above them runs in Rust, so a request that fails auth or a guard, hits a rate limit, or is a CORS preflight never acquires the GIL at all.

## Components

| Layer | Technology | Role |
| --- | --- | --- |
| HTTP server | [Actix Web](https://actix.rs/) on [Tokio](https://tokio.rs/) | Connection handling, HTTP parsing, response writing |
| Routing | [matchit](https://github.com/ibraheemdev/matchit) | Radix-tree router, no allocation per match |
| Python bridge | [PyO3](https://pyo3.rs/) | Calls handlers, converts arguments, receives the response tuple |
| Serialization | [msgspec](https://jcristharif.com/msgspec/) | Request validation and response encoding, 5–10× faster than `json` |
| Process model | `SO_REUSEPORT` | N independent processes, kernel load-balanced; each has its own interpreter and Django |
| Supervisor | `runbolt` | Spawns processes, recycles by RSS/lifetime, respawns on crash, drains WebSockets on shutdown |

## Where Django fits

Your Django project is loaded once per process at startup (`django.setup()`), the same way `manage.py` does it. From then on:

- **ORM** — handlers call the ORM directly. Async handlers use `aget`/`afilter`/… or return a `QuerySet`, which Bolt evaluates on a bounded, vendor-aware thread pool. Sync handlers run on a thread pool with the ORM inline.
- **Admin** — mounted automatically as an ASGI sub-application at your admin prefix; Django's own middleware stack runs for it.
- **Django middleware** — opt in with `BoltAPI(django_middleware=True)` to run `settings.MIDDLEWARE` on API routes (sessions, CSRF, auth, messages…). Built-in Django middleware is executed directly; third-party middleware is wrapped safely.
- **Signals, settings, apps, static/media** — unchanged. Static and media files are served from Rust.

## Why it is fast

1. **Rust owns the hot path.** HTTP, routing, auth, guards, CORS, rate limiting, and compression never take the GIL.
2. **Registration-time precomputation.** Parameter extractors, dependency graphs, response metadata, and middleware are compiled once when routes register, not per request.
3. **Sync dispatch bypass.** Handlers that don't actually suspend (detected at registration by bytecode analysis) skip the async machinery entirely — one GIL acquisition, response built in the same block.
4. **Persistent worker loop.** Truly async handlers run on a process-lived loop serviced by Tokio; there is no per-request task/thread creation.
5. **Zero-copy responses.** Serialized bodies cross to Rust without a memcpy; common response shapes use static metadata with zero allocation.
6. **msgspec.** Validation and encoding are C-accelerated and schema-compiled.

Measured effect: [Benchmarks](benchmarks.md).

## What this is not

- **Not a WSGI/ASGI adapter.** There is no protocol translation layer; Rust calls Python functions.
- **Not a Django fork.** `django` is a normal dependency; Bolt is a Django app plus a Rust extension.
- **Not a subset.** No Django feature is removed — see the [FAQ](faq.md#is-django-bolt-a-stripped-down-version-of-django).

For the deep dive (dispatch outcomes, wire format, worker loop), read [`CLAUDE.md`](https://github.com/dj-bolt/django-bolt/blob/master/CLAUDE.md) and [`docs/PROFILING.md`](https://github.com/dj-bolt/django-bolt/blob/master/docs/PROFILING.md) in the repository.

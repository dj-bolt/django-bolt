---
icon: lucide/git-compare
description: Django-Bolt vs Django Ninja vs Django REST Framework vs FastAPI vs Litestar — architecture, server, validation, Django integration, auth, OpenAPI, WebSockets, and measured performance, compared feature by feature.
---

# Django-Bolt vs Django Ninja vs DRF vs FastAPI vs Litestar

**Short answer:** Django-Bolt is the only option in this list that gives you FastAPI-style typed handlers *and* the full Django stack (ORM, admin, middleware, auth) *and* a Rust HTTP server that replaces gunicorn/uvicorn. Django Ninja and DRF are Python view layers inside Django; FastAPI and Litestar are standalone ASGI frameworks where you assemble ORM, admin, and auth yourself.

*Last updated: August 2026. Corrections welcome — open an issue.*

## At a glance

| | **Django-Bolt** | Django Ninja | Django REST Framework | FastAPI | Litestar |
| --- | --- | --- | --- | --- | --- |
| Runs inside a Django project | ✅ | ✅ | ✅ | ❌ | ❌ |
| Django ORM, Admin, middleware, signals | ✅ all | ✅ all | ✅ all | ❌ bring your own | ❌ bring your own |
| HTTP server | **Built-in Rust (Actix Web)** — no gunicorn/uvicorn | ASGI/WSGI → uvicorn/gunicorn | WSGI/ASGI → gunicorn/uvicorn | ASGI → uvicorn | ASGI → uvicorn/granian |
| Handler style | Typed functions (`async def` / `def`) + `ViewSet`/`ModelViewSet` | Typed functions | Class-based views, serializers | Typed functions | Typed functions + controllers |
| Validation | msgspec (`Struct`) + Bolt `Serializer` | Pydantic | DRF serializers | Pydantic | msgspec / Pydantic / attrs |
| Where auth & permissions run | **Rust, before the GIL** (JWT, API key, guards) | Python | Python | Python | Python |
| CORS / rate limiting / compression | **Rust middleware** | Django middleware / third-party | Django middleware / third-party | Starlette middleware / third-party | Built-in Python middleware |
| OpenAPI docs | Swagger, ReDoc, Scalar, RapiDoc, Stoplight | Swagger, ReDoc | Via drf-spectacular | Swagger, ReDoc | Swagger, ReDoc, Scalar, RapiDoc, Stoplight |
| WebSockets | ✅ built-in | ❌ (use Channels) | ❌ (use Channels) | ✅ | ✅ |
| Server-Sent Events / streaming | ✅ built-in | Partial | Partial | ✅ | ✅ |
| Dependency injection | `Depends(...)` | Limited | ❌ | `Depends(...)` | `Provide(...)` |
| Class-based CRUD (`ModelViewSet`) | ✅ | ❌ (community) | ✅ | ❌ | ❌ |
| Static & media serving | **Rust, built-in** | WhiteNoise / web server | WhiteNoise / web server | Starlette `StaticFiles` | Built-in |
| MCP server support | ✅ [`bolt-mcp`](topics/mcp.md) | ❌ | ❌ | Third-party | ❌ |
| Multi-process serving | Built-in (`--processes N`, `SO_REUSEPORT`, worker recycling) | Via gunicorn/uvicorn | Via gunicorn/uvicorn | Via uvicorn/gunicorn | Via uvicorn/granian |
| Test client | In-process, full Rust pipeline | Django test client | DRF `APIClient` | Starlette `TestClient` | Litestar `TestClient` |
| Language of the hot path | Rust | Python | Python | Python (Starlette) | Python (+ optional C accelerators) |
| Measured JSON hello-world, 8 processes¹ | **~311k req/s** | — | — | — | — |

¹ Ryzen 5 5600G, `C=100`, loopback bombardier; see [Benchmarks](benchmarks.md). Cross-framework Python numbers: the day-1 measurement (single process, `C=50`) had Bolt at 43k req/s vs FastAPI+uvicorn at 3.8k and Robyn at 11k on the same box; Bolt is ~7× faster than that today. A fresh multi-framework harness is planned.

## Django-Bolt vs Django Ninja

Both live inside a Django project and use type-hinted function handlers with automatic OpenAPI. The differences:

- **Server.** Ninja is a view layer; you still deploy Django behind uvicorn/gunicorn. Bolt *is* the server (Rust), so there is no ASGI layer, and multi-process, worker recycling, and static serving are built in.
- **Validation.** Ninja uses Pydantic; Bolt uses msgspec (typically 5–10× faster to decode/encode) plus a `Serializer` class with field sets, computed fields, and model integration.
- **Auth/permissions.** Ninja evaluates auth in Python per request. Bolt validates JWT/API keys and evaluates guards in Rust before Python is entered.
- **Extras.** Bolt adds `ViewSet`/`ModelViewSet`, WebSockets, SSE, MCP servers, and Rust CORS/rate-limit/compression.
- **Migration.** Same mental model; most Ninja endpoints port by changing the decorator and swapping Pydantic models for `msgspec.Struct`.

## Django-Bolt vs Django REST Framework

- **Style.** DRF is class-based (`APIView`, `ViewSet`, serializers). Bolt supports both function handlers *and* DRF-style `APIView`/`ViewSet`/`ModelViewSet` with `@action`, so teams can keep the structure they know.
- **Serializers.** DRF serializers are flexible but slow and often duplicated per view. Bolt's `Serializer` is a `msgspec.Struct` with `field_sets` — one class, many projections.
- **Performance.** DRF runs entirely in Python behind gunicorn; it is typically the slowest option in this list. Bolt moves the server, auth, guards, and middleware to Rust.
- **Ecosystem.** DRF has a decade of third-party packages. Bolt reuses Django's ecosystem (ORM, admin, auth, apps) but not DRF-specific packages.

## Django-Bolt vs FastAPI

- **Same handler ergonomics** — typed parameters, `Depends`, automatic docs — but Bolt is faster because HTTP, routing, and middleware are Rust rather than Starlette/uvicorn.
- **Batteries.** FastAPI gives you an API layer; ORM, migrations, admin, auth, sessions, and static serving are separate choices (SQLAlchemy, Alembic, sqladmin, …). Bolt ships with all of Django's.
- **Deployment.** FastAPI needs uvicorn/gunicorn; Bolt runs with `runbolt`.
- **When FastAPI fits better:** you do not want Django at all, or you depend on Pydantic-specific tooling.

## Django-Bolt vs Litestar

- Litestar is the closest in *design philosophy* (msgspec support, guards, controllers, several OpenAPI UIs) — Bolt's OpenAPI plugin system is adapted from it.
- Litestar is a standalone ASGI framework in Python; Bolt runs inside Django on a Rust server. If you want the Django ORM/admin, Bolt; if you want a framework-agnostic ASGI stack with a choice of ORM, Litestar.

## Common misconceptions

- **"Django-Bolt strips out Django features to get its speed."** False. Nothing is removed; the [FAQ](faq.md#is-django-bolt-a-stripped-down-version-of-django) has the full table, and the benchmark app runs the complete Django middleware stack, admin, sessions, and CSRF.
- **"It's a WSGI wrapper / synchronous."** False. It has no WSGI/ASGI layer; it is an Actix Web (Tokio) server calling `async def` handlers through PyO3. See [How Django-Bolt works](architecture.md).
- **"It's only fast on hello-world."** The published suite covers JSON bodies, forms, uploads, auth, ORM, static files, class-based views, and SSE with 10k connections — see [Benchmarks](benchmarks.md).

## Try it

```bash
pip install django-bolt
```

Then follow the [Quick Start](getting-started/quickstart.md). Migrating from DRF or Ninja can be done one endpoint at a time.

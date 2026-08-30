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
| Handler style | Typed functions (`async def` / `def`) + `ViewSet`/`ModelViewSet` | Typed functions | `@api_view` functions or class-based views; untyped | Typed functions | Typed functions + controllers |
| Validation | msgspec (`Struct`) + Bolt `Serializer` | Pydantic | DRF serializers | Pydantic | msgspec / Pydantic / attrs |
| Where auth & permissions run | **Rust, before the GIL** (JWT, API key, guards) | Python | Python | Python | Python |
| CORS / rate limiting / compression | **Rust middleware** | Django middleware / third-party | Django middleware / third-party | Starlette middleware / third-party | Built-in Python middleware |
| OpenAPI docs | Swagger, ReDoc, Scalar, RapiDoc, Stoplight | Swagger, ReDoc | Via drf-spectacular | Swagger, ReDoc | Swagger, ReDoc, Scalar, RapiDoc, Stoplight |
| WebSockets | ✅ built-in | ❌ (use Channels) | ❌ (use Channels) | ✅ | ✅ |
| Server-Sent Events / streaming | ✅ built-in | Partial | Partial | ✅ | ✅ |
| Dependency injection | `Depends(...)` | ❌ | ❌ | `Depends(...)` | `Provide(...)` |
| Class-based CRUD (`ModelViewSet`) | ✅ | ❌ (community) | ✅ | ❌ | ❌ |
| Static & media serving | **Rust, built-in** | WhiteNoise / web server | WhiteNoise / web server | Starlette `StaticFiles` | Built-in |
| MCP server support | ✅ [`bolt-mcp`](topics/mcp.md) | ❌ | ❌ | Third-party | ❌ |
| Multi-process serving | Built-in (`--processes N`, `SO_REUSEPORT`, worker recycling) | Via gunicorn/uvicorn | Via gunicorn/uvicorn | Via uvicorn/gunicorn | Via uvicorn/granian |
| Test client | In-process, full Rust pipeline | `ninja.testing.TestClient` | DRF `APIClient` | Starlette `TestClient` | Litestar `TestClient` |
| Language of the hot path | Rust | Python | Python | Python (Starlette) | Python (msgspec in C) |
| Maturity (first release, version) | 2025, 0.x | 2020, 1.x | 2011, 3.x | 2018, 0.x | 2021, 2.x |
| Ecosystem and community | Small, no third-party packages yet | Medium | Large, hundreds of packages | Large, hundreds of packages | Medium |
| Install | Rust extension (prebuilt wheels) | Pure Python | Pure Python | Pure Python | Pure Python |
| Measured JSON hello-world, 8 processes¹ | **~311k req/s** | — | — | — | — |

¹ Ryzen 5 5600G, `C=100`, loopback bombardier; see [Benchmarks](benchmarks.md). Cross-framework, one process each, Python 3.14 (2026-08-30): Django-Bolt 43.5k req/s on 1 KB JSON vs Litestar 15.5k and FastAPI 7.5k; on a 10-row PostgreSQL query 2.9k vs 1.3k and 1.2k. Full table in [Benchmarks](benchmarks.md#against-python-frameworks).

## Django-Bolt vs Django Ninja

Both live inside Django and use type-hinted handlers with automatic OpenAPI.

- **Server.** Ninja is a view layer behind uvicorn/gunicorn. Bolt *is* the server, with multi-process, worker recycling, and static serving built in.
- **Validation.** Ninja uses Pydantic. Bolt uses msgspec ([benchmarks](https://jcristharif.com/msgspec/benchmarks.html)) plus a `Serializer` with field sets, computed fields, and model integration.
- **Auth.** Ninja checks auth in Python. Bolt validates JWT/API keys and guards in Rust before Python runs.
- **Extras.** `ViewSet`/`ModelViewSet`, WebSockets, SSE, MCP servers, Rust CORS/rate-limit/compression.
- **Migration.** Change the decorator, swap Pydantic models for `msgspec.Struct`. Ninja routes keep serving through the [ASGI mount](topics/asgi-mounts.md) while you move.
- **Pick Ninja** if you want Pydantic, a pure-Python install, or a 1.x API.

## Django-Bolt vs Django REST Framework

- **Style.** DRF has `@api_view` functions and class views, but neither reads type hints; validation lives in serializers. Bolt validates from the handler signature and also offers `APIView`/`ViewSet`/`ModelViewSet` with `@action`.
- **Serializers.** DRF serializers are flexible but slow. Bolt's `Serializer` is a `msgspec.Struct` with `field_sets`: one class, many projections.
- **Performance.** DRF runs entirely in Python behind gunicorn. Bolt moves the server, auth, guards, and middleware to Rust.
- **Ecosystem.** DRF has more than a decade of packages (django-filter, dj-rest-auth, drf-spectacular, simplejwt). Bolt has none yet; it reuses Django's ecosystem, not DRF's.
- **Migration.** DRF views keep working through the [ASGI mount](topics/asgi-mounts.md). Move one endpoint at a time.
- **Pick DRF** if you depend on its packages or need a stable, long-lived API.

## Django-Bolt vs FastAPI

- **Same handler ergonomics**: typed parameters, `Depends`, automatic docs. Bolt is faster because HTTP, routing, and middleware are Rust, not Starlette/uvicorn.
- **Batteries.** FastAPI is an API layer; ORM, migrations, admin, auth, and static files are separate choices. Bolt ships Django's.
- **Deployment.** FastAPI needs uvicorn/gunicorn. Bolt runs with `runbolt`.
- **Pick FastAPI** if you do not want Django, or depend on Pydantic tooling or FastAPI's package ecosystem.

## Django-Bolt vs Litestar

- Closest in design: msgspec, guards, controllers, several OpenAPI UIs. Bolt's OpenAPI plugin system is adapted from Litestar.
- Litestar is a standalone ASGI framework; Bolt runs inside Django on a Rust server.
- **Pick Litestar** if you want a framework-agnostic ASGI stack, a choice of ORM and validation library, or a stable 2.x API.

## Where Django-Bolt is weaker

- **Young.** First release 2025, version 0.x. Minor releases can change APIs; read the [changelog](https://github.com/dj-bolt/django-bolt/blob/master/CHANGELOG.md) before you upgrade.
- **No package ecosystem.** Filters, social auth, or API versioning packages do not exist yet. You write or adapt them.
- **Small community.** Fewer tutorials and answered questions. Skills transfer: anyone who knows Django plus FastAPI or Ninja is productive in a day.
- **Rust extension.** Prebuilt wheels cover common platforms; others need a Rust toolchain. Rust crashes are harder to debug than Python ones.
- **Not ASGI.** Bolt cannot run under uvicorn, gunicorn, or serverless ASGI adapters. Bolt is the server.
- **Not Pydantic.** Bolt's `Serializer` (validators, computed fields, nested, `from_model`) sits on msgspec. Pydantic-specific packages and `BaseModel` classes need a port.
- **Django middleware is opt-in.** `BoltAPI(django_middleware=True)` enables it and costs throughput. The published benchmarks run without it.

## Common misconceptions

- **"Django-Bolt strips out Django features."** False. Nothing is removed; see the [FAQ](faq.md#is-django-bolt-a-stripped-down-version-of-django).
- **"It's a WSGI wrapper / synchronous."** False. It is an Actix Web (Tokio) server that calls `async def` and `def` handlers through PyO3, and can mount ASGI apps. See [How Django-Bolt works](architecture.md).
- **"It's only fast on hello-world."** The suite covers JSON bodies, forms, uploads, auth, ORM, static files, class-based views, and SSE with 10k connections. See [Benchmarks](benchmarks.md).

## Try it

```bash
pip install django-bolt
```

Then follow the [Quick Start](getting-started/quickstart.md). Migrate from DRF or Ninja one endpoint at a time; the old routes keep serving next to the new ones.

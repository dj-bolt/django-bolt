---
description: Django-Bolt next to Django Ninja, Django REST Framework, FastAPI and Litestar. Server, validation, Django integration, features, speed and weak points.
---

# Comparison

This page compares Django-Bolt with four Python API frameworks.
It also lists where Bolt is weaker, so that you can choose with all the facts.

Last checked: November 2026. If a fact here is wrong, [open an issue](https://github.com/dj-bolt/django-bolt/issues).

## In one paragraph

Django Ninja and Django REST Framework (DRF) are API layers inside Django. They need a separate server, such as gunicorn or uvicorn.
FastAPI and Litestar are standalone ASGI frameworks. You choose and connect your own ORM, migrations, admin and auth.
Django-Bolt runs inside Django, like Ninja and DRF, and has typed handlers, like FastAPI.
It also brings its own HTTP server, written in Rust, and runs auth, CORS and rate limits there.

## Features

| | Django-Bolt | Django Ninja | DRF | FastAPI | Litestar |
| --- | --- | --- | --- | --- | --- |
| Runs inside a Django project | Yes | Yes | Yes | No | No |
| Django ORM, admin, migrations | Yes | Yes | Yes | No. Choose your own. | No. Choose your own. |
| HTTP server | Built in (Rust, Actix Web) | gunicorn or uvicorn | gunicorn or uvicorn | uvicorn or another ASGI server | uvicorn or another ASGI server |
| Handler style | Typed functions, class-based views | Typed functions | Untyped functions and classes | Typed functions | Typed functions, controllers |
| Validation | msgspec | Pydantic | DRF serializers | Pydantic | msgspec, Pydantic or attrs |
| Where auth and guards run | Rust | Python | Python | Python | Python |
| CORS, rate limits, compression | Built in, Rust | Django middleware or packages | Django middleware or packages | Starlette middleware or packages | Built in, Python |
| Dependency injection | `Depends()` | No | No | `Depends()` | `Provide()` |
| Model viewsets (CRUD classes) | `ModelViewSet` | No | `ModelViewSet` | No | No |
| Pagination | Built in | Built in | Built in | Packages | Built in |
| OpenAPI UIs | Swagger UI, ReDoc, Scalar, RapiDoc, Stoplight Elements | Swagger UI, ReDoc | Packages (drf-spectacular) | Swagger UI, ReDoc | Swagger UI, ReDoc, Scalar, RapiDoc, Stoplight Elements |
| WebSocket | Built in | No. Use Django Channels. | No. Use Django Channels. | Built in | Built in |
| Server-sent events | Built in | Django `StreamingHttpResponse` | Django `StreamingHttpResponse` | Built in | Built in |
| Static and media files | Built in, Rust | WhiteNoise or a web server | WhiteNoise or a web server | Starlette `StaticFiles` | Built in |
| MCP servers | Built in ([`bolt-mcp`](topics/mcp.md)) | Packages | Packages | Packages | Packages |
| Many processes | `runbolt --processes N` | Through the server | Through the server | Through the server | Through the server |
| First release, version | 2025, 0.x | 2020, 1.x | 2011, 3.x | 2018, 0.x | 2021, 2.x |
| Third-party packages | Few | Some | Many | Many | Some |
| Install | Wheels with a Rust extension | Pure Python | Pure Python | Pure Python | Pure Python |

## Speed

The [benchmark harness](benchmarks.md#against-litestar-and-fastapi) runs the same routes on Bolt, Litestar and FastAPI, with one process for each.

| Route | Django-Bolt | Litestar | FastAPI |
| --- | ---: | ---: | ---: |
| 1 KB JSON response | 43,541 | 15,477 | 7,517 |
| 10 rows from PostgreSQL | 2,881 | 1,321 | 1,237 |
| JWT auth, then load the user | 4,478 | 1,155 | 941 |

Requests per second, 100 connections, Python 3.14, 2026-08-30. [Benchmarks](benchmarks.md) has the conditions.
We have not measured Django Ninja and DRF with the same harness.

## Django-Bolt and Django Ninja

The two have the most in common. Both run in Django, and both read parameters from type hints.

- **Server.** Ninja is a view layer behind gunicorn or uvicorn. Bolt is the server. It also runs many processes, recycles workers and serves static files.
- **Validation.** Ninja uses Pydantic. Bolt uses msgspec, and a `Serializer` class for model data.
- **Auth.** Ninja checks auth in Python. Bolt checks JWT, API keys and guards in Rust.
- **Extra features.** Bolt has class-based views and viewsets, `Depends()`, WebSocket, server-sent events and MCP.
- **Choose Ninja** if you need Pydantic, a pure-Python install, or a 1.x release.

## Django-Bolt and Django REST Framework

- **Style.** DRF validates in serializers, not in the handler signature. Bolt reads types from the signature. It also has `APIView`, `ViewSet`, `ModelViewSet` and `@action`, with names close to DRF.
- **Speed.** DRF runs in Python behind a Python server. Bolt moves the server, auth and guards to Rust.
- **Packages.** DRF has more than ten years of packages: django-filter, drf-spectacular, Simple JWT and many more. Bolt cannot use them.
- **Choose DRF** if you depend on its packages, or need a long-stable API.

## Django-Bolt and FastAPI

- **Handlers.** Both use typed parameters, `Depends()` and generated docs.
- **Scope.** FastAPI is an API layer. You choose the ORM, migrations, admin, auth and static files. Bolt uses Django for these.
- **Server.** FastAPI needs uvicorn or another ASGI server. Bolt has its own server.
- **Choose FastAPI** if you do not want Django, or you depend on Pydantic or FastAPI packages.

## Django-Bolt and Litestar

- **Design.** Both use msgspec, guards and several OpenAPI UIs. The OpenAPI plugins of Bolt are adapted from Litestar.
- **Scope.** Litestar is a standalone ASGI framework. Bolt runs inside Django.
- **Choose Litestar** if you want an ASGI stack without Django, or a choice of ORM and validation library.

## Where Django-Bolt is weaker

- **It is young.** The first release was in 2025. The version is 0.x, so a minor release can change an API. Read the [changelog](https://github.com/dj-bolt/django-bolt/blob/master/CHANGELOG.md) before you upgrade.
- **It has few packages.** Packages for filters, social login or API versions do not exist yet. Django packages that do not depend on DRF or Ninja still work.
- **The community is small.** There are fewer tutorials and fewer answered questions.
- **It has a Rust extension.** Wheels cover the common platforms. Other platforms need a Rust toolchain to install. A crash in Rust is harder to debug than a Python error.
- **It is not ASGI.** You cannot run Bolt routes under uvicorn, gunicorn or a serverless ASGI adapter.
- **It is not Pydantic.** Pydantic models and packages that need them must change to msgspec.
- **Django middleware costs throughput.** It is off by default on Bolt routes. When you turn it on, each request runs the full stack in Python.

## Move to Django-Bolt

You can move one endpoint at a time. Your old views keep working next to the new routes.
[Migrate from Ninja, DRF or FastAPI](topics/migrating.md) shows each step.

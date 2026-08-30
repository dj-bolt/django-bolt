---
icon: lucide/circle-help
description: Direct answers to the most common questions about Django-Bolt — production readiness, Django Admin and ORM support, WSGI vs async, gunicorn, and how it compares to Django Ninja, DRF and FastAPI.
---

# Frequently asked questions

Short, direct answers. Each answer links to the page with the full detail.

## Is Django-Bolt a stripped-down version of Django?

**No.** Django-Bolt removes nothing from Django. It *replaces the HTTP server and the API layer* — the part that WSGI/ASGI servers, DRF, or Django Ninja would otherwise handle — with a Rust server (Actix Web) and a typed, msgspec-based routing layer. Everything else is the same Django you already run:

| Django feature | In Django-Bolt | Notes |
| --- | --- | --- |
| Django ORM | ✅ Full | Sync and async (`aget`, `afilter`, …); return a `QuerySet` from an async handler and Bolt evaluates it on a bounded thread pool. [Async ORM](topics/async-orm.md) |
| Django Admin | ✅ Full | Auto-mounted from `INSTALLED_APPS`; served by `runbolt`, no second server. |
| Django middleware | ✅ Full | `BoltAPI(django_middleware=True)` runs your `settings.MIDDLEWARE` stack (sessions, CSRF, auth, messages, CSP…) on API routes. [Middleware](topics/middleware.md#django-middleware-integration) |
| Django auth / sessions | ✅ Full | JWT and API-key backends are built in (validated in Rust); with `django_middleware=True`, Django's `SessionMiddleware` + `AuthenticationMiddleware` populate `request.user` / `request.session` exactly as in Django. [Authentication](topics/authentication.md) |
| Django signals | ✅ Full | `request_started`, `request_finished`, and model signals fire. [Signals](topics/signals.md) |
| Django settings / apps | ✅ Full | Same `settings.py`, `INSTALLED_APPS`, `manage.py`. |
| Static & media files | ✅ Full | Served natively from Rust — no WhiteNoise. [Static files](topics/static-files.md) |
| Third-party Django apps | ✅ Full | Anything that plugs into Django's ORM, admin, or middleware works. |
| Templates / server-rendered views | ✅ | Existing Django URLconf views run through the ASGI mount alongside API routes. [ASGI mounts](topics/asgi-mounts.md) |

The performance comes from *where* work runs (routing, auth, guards, CORS, rate limiting, compression in Rust without the GIL) — not from removing features. Django middleware is opt-in per API (`BoltAPI(django_middleware=True)`); the [example project](https://github.com/dj-bolt/django-bolt/tree/master/python/example) used for the published benchmarks runs the admin and sessions but not `settings.MIDDLEWARE` on Bolt routes.

## Does Django-Bolt use WSGI? Is it synchronous?

**No WSGI, no ASGI server, not sync-only.** Django-Bolt ships its own HTTP server: [Actix Web](https://actix.rs/) on the Tokio runtime, called from `python manage.py runbolt`. Handlers are `async def` (sync `def` is also supported). Requests flow HTTP → Rust → your Python handler via PyO3, with no WSGI or ASGI protocol layer in between. See [How Django-Bolt works](architecture.md).

## Do I need gunicorn or uvicorn?

**No.** `runbolt` *is* the production server. It runs multiple processes with `SO_REUSEPORT` kernel load balancing, recycles workers by RSS or lifetime, respawns on crash, and drains WebSockets on shutdown. See [Deployment](getting-started/deployment.md).

```bash
python manage.py runbolt --host 0.0.0.0 --port 8000 --processes 4
```

## Is Django-Bolt production ready?

Django-Bolt is used in production, publishes a benchmark suite and regression gate for every release, and has a full test suite covering routing, auth, middleware, ORM, streaming, WebSockets, and multi-process behavior. It is pre-1.0, so minor releases can still change APIs; the [changelog](https://github.com/dj-bolt/django-bolt/blob/master/CHANGELOG.md) lists every breaking change.

## How fast is Django-Bolt?

On a 12-core desktop (Ryzen 5 5600G, 8 processes, `C=100`, loopback) Django-Bolt serves **~311,000 requests/second** on a JSON hello-world, ~187,000 req/s for a 10 KB JSON response, and ~21,000–27,000 req/s for a 10-row Django ORM query on SQLite. It is faster than FastAPI, Robyn, and — on 10 KB payloads — faster than Bun-based Elysia and Hono. Numbers, conditions, and reproduction commands are on the [Benchmarks](benchmarks.md) page.

## How does Django-Bolt compare to Django Ninja and Django REST Framework?

All three run inside a Django project. DRF and Django Ninja are Python view layers that still need gunicorn/uvicorn; Django-Bolt replaces the server with Rust and moves auth, guards, and middleware out of Python. Bolt's syntax is closest to Django Ninja/FastAPI (type-hinted function handlers) and it also provides DRF-style `ViewSet`/`ModelViewSet` classes. Full grid: [Comparison](comparison.md).

## How does Django-Bolt compare to FastAPI and Litestar?

FastAPI and Litestar are standalone ASGI frameworks — you bring your own ORM, admin, auth, and migrations. Django-Bolt gives you the same type-hinted handler style plus the whole Django stack, and serves requests faster because the HTTP layer is Rust. See [Comparison](comparison.md).

## Can I migrate from DRF or Django Ninja incrementally?

**Yes.** Add `django_bolt` to `INSTALLED_APPS`, create an `api.py`, and move one endpoint at a time. Existing Django URLconf views (including DRF/Ninja routes) keep working through the ASGI mount while you migrate.

## Does Django-Bolt support WebSockets, SSE, and streaming?

Yes — [WebSockets](topics/websocket.md), [Server-Sent Events](topics/sse.md), and [streaming responses](topics/responses.md) are built in and served by the Rust layer.

## Does Django-Bolt generate OpenAPI docs?

Yes. Swagger UI, ReDoc, Scalar, RapiDoc, and Stoplight Elements are served at `/docs` by default. See [OpenAPI](topics/openapi.md).

## Which Python and Django versions are supported?

Python 3.12, 3.13, 3.14 (CPython and PyPy). Django 4.2, 5.0, 5.1, 5.2, 6.0.

## Where can I get help?

[Discord](https://discord.gg/4xErptXK82) · [GitHub Issues](https://github.com/dj-bolt/django-bolt/issues) · [DeepWiki](https://deepwiki.com/FarhanAliRaza/django-bolt)

<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "FAQPage",
 "mainEntity": [
  {
   "@type": "Question",
   "name": "Is Django-Bolt a stripped-down version of Django?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "No. Django-Bolt removes nothing from Django. It replaces the HTTP server and the API layer — the part that WSGI/ASGI servers, DRF, or Django Ninja would otherwise handle — with a Rust server (Actix Web) and a typed, msgspec-based routing layer. Everything else is the same Django you already run:"
   }
  },
  {
   "@type": "Question",
   "name": "Does Django-Bolt use WSGI? Is it synchronous?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "No WSGI, no ASGI server, not sync-only. Django-Bolt ships its own HTTP server: Actix Web on the Tokio runtime, called from python manage.py runbolt. Handlers are async def (sync def is also supported). Requests flow HTTP → Rust → your Python handler via PyO3, with no WSGI or ASGI protocol layer in between. See How Django-Bolt works."
   }
  },
  {
   "@type": "Question",
   "name": "Do I need gunicorn or uvicorn?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "No. runbolt is the production server. It runs multiple processes with SO_REUSEPORT kernel load balancing, recycles workers by RSS or lifetime, respawns on crash, and drains WebSockets on shutdown. See Deployment."
   }
  },
  {
   "@type": "Question",
   "name": "Is Django-Bolt production ready?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Django-Bolt is used in production, publishes a benchmark suite and regression gate for every release, and has a full test suite covering routing, auth, middleware, ORM, streaming, WebSockets, and multi-process behavior. It is pre-1.0, so minor releases can still change APIs; the changelog lists every breaking change."
   }
  },
  {
   "@type": "Question",
   "name": "How fast is Django-Bolt?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "On a 12-core desktop (Ryzen 5 5600G, 8 processes, C=100, loopback) Django-Bolt serves ~311,000 requests/second on a JSON hello-world, ~187,000 req/s for a 10 KB JSON response, and ~21,000–27,000 req/s for a 10-row Django ORM query on SQLite. It is faster than FastAPI, Robyn, and — on 10 KB payloads — faster than Bun-based Elysia and Hono. Numbers, conditions, and reproduction commands are on the Benchmarks page."
   }
  },
  {
   "@type": "Question",
   "name": "How does Django-Bolt compare to Django Ninja and Django REST Framework?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "All three run inside a Django project. DRF and Django Ninja are Python view layers that still need gunicorn/uvicorn; Django-Bolt replaces the server with Rust and moves auth, guards, and middleware out of Python. Bolt's syntax is closest to Django Ninja/FastAPI (type-hinted function handlers) and it also provides DRF-style ViewSet/ModelViewSet classes. Full grid: Comparison."
   }
  },
  {
   "@type": "Question",
   "name": "How does Django-Bolt compare to FastAPI and Litestar?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "FastAPI and Litestar are standalone ASGI frameworks — you bring your own ORM, admin, auth, and migrations. Django-Bolt gives you the same type-hinted handler style plus the whole Django stack, and serves requests faster because the HTTP layer is Rust. See Comparison."
   }
  },
  {
   "@type": "Question",
   "name": "Can I migrate from DRF or Django Ninja incrementally?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Yes. Add django_bolt to INSTALLED_APPS, create an api.py, and move one endpoint at a time. Existing Django URLconf views (including DRF/Ninja routes) keep working through the ASGI mount while you migrate."
   }
  },
  {
   "@type": "Question",
   "name": "Does Django-Bolt support WebSockets, SSE, and streaming?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Yes — WebSockets, Server-Sent Events, and streaming responses are built in and served by the Rust layer."
   }
  },
  {
   "@type": "Question",
   "name": "Does Django-Bolt generate OpenAPI docs?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Yes. Swagger UI, ReDoc, Scalar, RapiDoc, and Stoplight Elements are served at /docs by default. See OpenAPI."
   }
  },
  {
   "@type": "Question",
   "name": "Which Python and Django versions are supported?",
   "acceptedAnswer": {
    "@type": "Answer",
    "text": "Python 3.12, 3.13, 3.14 (CPython and PyPy). Django 4.2, 5.0, 5.1, 5.2, 6.0."
   }
  }
 ]
}
</script>

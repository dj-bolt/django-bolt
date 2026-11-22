---
description: Answers to common questions about Django-Bolt. Production use, Django features, WSGI and ASGI, servers, versions, and how to get help.
---

# FAQ

## Does Django-Bolt remove Django features?

No. Bolt replaces two parts of a Django project: the HTTP server and the API layer.
Everything else is the Django you know.

| Django feature | With Bolt |
| --- | --- |
| ORM, sync and async | Works. Return a `QuerySet` from an async handler, or use `aget()` and `afirst()`. See [Django ORM](topics/async-orm.md). |
| Admin | Works. `runbolt` serves it at the prefix in your URLconf. |
| Middleware | Off on Bolt routes by default. Turn it on with `BoltAPI(django_middleware=True)`. See [Middleware](topics/middleware.md). |
| Users, sessions, permissions | Works. Bolt adds JWT and API-key auth. See [Authentication](topics/authentication.md). |
| Signals | Works. See [Signals](topics/signals.md). |
| Settings and apps | No change. |
| Static and media files | Served by Bolt in Rust. See [Static files](topics/static-files.md). |
| Other Django views | Mount Django with `api.mount_django()`. See [ASGI mounts](topics/asgi-mounts.md). |

## Does Bolt use WSGI or ASGI?

No. `runbolt` starts an HTTP server written in Rust. The server calls your handlers through PyO3, with no WSGI or ASGI layer.
Handlers can be `async def` or `def`. See [How it works](architecture.md).

Bolt can mount an ASGI application, for example the Django admin. See [ASGI mounts](topics/asgi-mounts.md).

## Do I need gunicorn or uvicorn?

No. `runbolt` is the production server. It runs many processes. With options, it also recycles workers and restarts a worker that stops.

```bash
python manage.py runbolt --host 0.0.0.0 --port 8000 --processes 4
```

See [Deployment](topics/deployment.md).

## Can I use Bolt in production?

Projects use Bolt in production. The test suite covers routing, auth, middleware, the ORM, streaming, WebSocket and many processes.
Bolt is at version 0.x. A minor release can change an API.
Read the [changelog](https://github.com/dj-bolt/django-bolt/blob/master/CHANGELOG.md) before each upgrade, and pin the version in your requirements.

## How fast is Bolt?

On a 6-core desktop CPU with 8 processes and 100 connections, Bolt serves 324,493 requests per second for a small JSON route.
With one process each, it serves 2.8× the requests of Litestar and 5.8× the requests of FastAPI for a 1 KB JSON response.
See [Benchmarks](benchmarks.md) for all results and their conditions.

## Which Python and Django versions does Bolt support?

See [Installation](intro/install.md#requirements).

## Can I move from DRF or Django Ninja one endpoint at a time?

Yes. Keep your old views, and add Bolt routes next to them. See [Migrate from Ninja, DRF or FastAPI](topics/migrating.md).

## Does Bolt support WebSocket and server-sent events?

Yes. See [WebSocket](topics/websocket.md) and [Server-sent events](topics/sse.md).

## Does Bolt make OpenAPI docs?

Yes. Bolt serves the schema and Swagger UI, ReDoc, Scalar, RapiDoc and Stoplight Elements at `/docs`. See [OpenAPI](topics/openapi.md).

## Can I use Pydantic models?

No. Bolt validates with [msgspec](https://jcristharif.com/msgspec/). Use `msgspec.Struct` or the Bolt `Serializer`. See [Serializers](topics/serializers.md).

## Why does runbolt stop on macOS when it connects to PostgreSQL?

This problem occurs on macOS with libpq (psycopg), when `runbolt` starts worker processes with `fork()`.
It occurs with `--processes 2` or more, and with `--max-rss`, `--workers-lifetime` or `--respawn-failed-workers`.
A worker stops, or ends with a segmentation fault, when it opens a PostgreSQL connection.

The cause is GSSAPI encryption. When libpq has GSSAPI support, it calls the macOS Kerberos framework for each connection.
That framework is not safe to use after `fork()`.

Turn off GSSAPI encryption with an environment variable:

```bash
PGGSSENCMODE=disable python manage.py runbolt --processes 2
```

Or set the option in `DATABASES`:

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "app",
        "OPTIONS": {"gssencmode": "disable"},
    }
}
```

This setting turns off GSSAPI encryption only. To encrypt the connection, use TLS with `sslmode`, for example `verify-full`.
Linux does not need this setting.

## Where can I get help?

- Ask a question on [Discord](https://discord.gg/4xErptXK82).
- Report a bug on [GitHub Issues](https://github.com/dj-bolt/django-bolt/issues). Give the versions of Bolt, Django and Python, and a short example.

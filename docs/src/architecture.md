---
description: How a request moves through Django-Bolt, which parts run in Rust and which in Python, and where Django fits.
---

# How it works

Django-Bolt is a Django app and a Rust extension module.
`python manage.py runbolt` starts an HTTP server written in Rust.
The server calls your Python handlers directly. There is no WSGI or ASGI layer between them.

This page is for readers who want to know why Bolt behaves as it does.
You do not need it to build an API.

## The path of a request

```text
 client
   │
   ▼
 Actix Web (Rust) ─ parses HTTP/1.1, keeps connections alive
   │
   ▼
 Router (Rust) ─ finds the route for the method and path
   │
   ▼
 Rust middleware ─ CORS, rate limits
   │
   ▼
 Authentication and guards (Rust) ─ JWT, API keys, IsAuthenticated, Requires(...)
   │
   ▼
 Your handler (Python) ─ typed arguments, Django ORM, your code
   │
   ▼
 Response (Rust) ─ msgspec bytes, headers, cookies, compression
   │
   ▼
 client
```

Only the handler step runs Python.
Bolt answers a CORS preflight, a request over the rate limit, or a request with a bad token in Rust.
Such a request does not take the Python global interpreter lock (GIL).

## What runs where

| Step | Language | What it does |
| --- | --- | --- |
| HTTP server | Rust ([Actix Web](https://actix.rs/), [Tokio](https://tokio.rs/)) | Accepts connections, parses requests, writes responses. |
| Routing | Rust ([matchit](https://github.com/ibraheemdev/matchit)) | Matches the path with a radix tree. |
| CORS, rate limits | Rust | Reads the configuration that Bolt compiles from your settings and decorators at startup. |
| Authentication, guards | Rust | Checks JWT signatures and expiry, API keys, and the guards of the route. |
| Parameter parsing | Rust and Python | Rust parses the query string, headers, cookies and forms. msgspec decodes and validates the JSON body. |
| Handler | Python | Your code. |
| Serialization | Python ([msgspec](https://jcristharif.com/msgspec/)) | Encodes the return value to JSON bytes. |
| Response | Rust | Builds headers and cookies, compresses the body, writes it. |
| Static and media files | Rust | Serves `STATIC_URL` and `MEDIA_URL` without Python. |

## Processes and threads

`runbolt --processes N` starts N processes.
Each process loads Django once and has its own Python interpreter.
On Linux, all processes listen on the same port with `SO_REUSEPORT`, and the kernel spreads the connections.

Each process has one or more Actix worker threads (`--workers`).
Each worker thread owns an asyncio event loop. An `async def` handler runs on the loop of the thread that received the request.

On a normal (GIL) build of Python, add processes to use more CPU cores. Do not add worker threads.
See [Deployment](topics/deployment.md) for sizing.

## Sync and async handlers

Bolt chooses how to call a handler when you register the route. It does not choose per request.

- A `def` handler runs in one call, with the GIL held for the whole call. Bolt builds the response in the same step.
- An `async def` handler that never awaits also runs in one call. Bolt finds this from the bytecode of the function.
- An `async def` handler that awaits runs as a task on the event loop of the worker thread.

The Django ORM is sync code. A `def` handler can call it directly.
An `async def` handler can use the async ORM methods (`aget`, `afirst`, `acreate`), or return a `QuerySet`.
Bolt runs a returned `QuerySet` on a bounded thread pool. See [Django ORM](topics/async-orm.md).

## Where Django fits

Bolt calls `django.setup()` once in each process, as `manage.py` does. Then:

- **Models and the ORM** work as in any Django project.
- **The admin** is served by `runbolt` at the prefix in your URLconf. Bolt mounts Django's ASGI handler for it. Use `runbolt --no-admin` to turn this off.
- **Django middleware** does not run on Bolt routes by default. Turn it on with `BoltAPI(django_middleware=True)`. A route with Django middleware runs on its own thread for each request, so thread-local state stays with the request. See [Middleware](topics/middleware.md).
- **Other Django views** can run next to Bolt routes. Mount Django's ASGI handler with `api.mount_django(...)`. See [ASGI mounts](topics/asgi-mounts.md).
- **Settings, apps, signals and templates** do not change.

## Why it is fast

1. **Rust does the work around the handler.** Parsing, routing, auth, guards, CORS, rate limits and compression run without Python.
2. **Work happens once, at startup.** Bolt inspects each handler signature when you register the route. It builds the parameter extractors, the dependency graph and the response metadata then. A request reuses them.
3. **Simple handlers skip the event loop.** A handler that does not await runs in one call, with no task and no thread switch.
4. **The event loop lives as long as the process.** Bolt does not create a loop, a task group or a thread for each request.
5. **The response body is not copied.** The bytes from msgspec go to Rust as they are.

[Benchmarks](benchmarks.md) gives the measured effect.

## What Bolt is not

- **Not a WSGI or ASGI application.** You cannot run Bolt routes under gunicorn or uvicorn. `runbolt` is the server.
- **Not a fork of Django.** Django is a normal dependency. Bolt removes no Django feature.
- **Not Pydantic.** Validation uses msgspec. See [Serializers](topics/serializers.md).

## See also

- [Deployment](topics/deployment.md): processes, workers and database connections.
- [Benchmarks](benchmarks.md): numbers, conditions and how to reproduce them.
- [`CLAUDE.md`](https://github.com/dj-bolt/django-bolt/blob/master/CLAUDE.md) and [`docs/PROFILING.md`](https://github.com/dj-bolt/django-bolt/blob/master/docs/PROFILING.md) in the repository: design notes for contributors.

---
description: Run code when runbolt starts and stops, with an async context manager that you pass to BoltAPI.
---

# Lifespan

A lifespan is an async context manager. Django-Bolt enters it before the server starts and exits it after the server stops. Use it to load data, to check a service, or to start and stop a background task.

## Add a lifespan

Write a function with `@asynccontextmanager`, and pass it to `BoltAPI(lifespan=...)`. The code before `yield` runs at startup. The code after `yield` runs at shutdown:

```python title="news/api.py"
from contextlib import asynccontextmanager

from django_bolt import BoltAPI

from blog.models import Article

# The titles of the published articles, loaded once for each process.
published_titles: set[str] = set()


@asynccontextmanager
async def lifespan(app: BoltAPI):
    async for title in Article.objects.filter(published=True).values_list("title", flat=True):
        published_titles.add(title)
    print(f"Loaded {len(published_titles)} published titles")
    yield
    published_titles.clear()
    print("Cleared the title cache")


api = BoltAPI(lifespan=lifespan)


@api.get("/titles/check")
async def check_title(title: str) -> dict:
    return {"title": title, "taken": title in published_titles}
```

```bash
python manage.py runbolt
```

The last lines of the output, after one request and Ctrl+C:

```text
  press CTRL+C to stop

Loaded 2 published titles
INFO - 2026-11-21 20:13:27 - django.server - GET /titles/check 200 0.3ms
Cleared the title cache
```

Bolt calls the function with one argument: the `BoltAPI` that has the lifespan.

The lifespan runs in an event loop. Use the async ORM in it, for example `async for`, `acount()` and `aget()`. A sync ORM call there raises `SynchronousOnlyOperation`. Django is ready when the lifespan starts: the apps are loaded, and `AppConfig.ready()` has run.

The function must be async. A `@contextmanager` function raises `TypeError` at startup.

## Clean up when something fails

Put the shutdown code in a `finally` block:

```python
@asynccontextmanager
async def lifespan(app: BoltAPI):
    published_titles.update([title async for title in Article.objects.values_list("title", flat=True)])
    try:
        yield
    finally:
        published_titles.clear()
```

The code after a plain `yield` does not run when an error occurs. Errors can occur in these cases:

- The startup of a lifespan raises an error. `runbolt` then exits with status 1 and serves no request. The lifespans that started before it get the error at their `yield`.
- The server stops with an error.

On `SIGTERM`, the server stops accepting connections and lets the requests in progress end. Then the shutdown code runs. On `SIGINT` (Ctrl+C), the server stops at once, and the shutdown code runs. On `SIGKILL` or a crash, no shutdown code runs.

## Run a background task

A task that the lifespan starts runs on the event loop of the lifespan. `runbolt` keeps that loop running while the server runs:

```python
import asyncio


async def refresh_titles():
    while True:
        await asyncio.sleep(60)
        titles = {title async for title in Article.objects.filter(published=True).values_list("title", flat=True)}
        # Add the new titles, then remove the old ones. A handler never sees an empty set.
        published_titles.update(titles)
        published_titles.intersection_update(titles)


@asynccontextmanager
async def lifespan(app: BoltAPI):
    task = asyncio.create_task(refresh_titles())
    try:
        yield
    finally:
        task.cancel()
```

## Use objects that belong to an event loop

The lifespan runs on its own event loop. Handlers run on the event loops of the Bolt worker threads, one loop for each thread. Some objects work only on the loop that made them. Examples are an `asyncio.Future`, an `asyncio.Lock`, and the connection pool of an async database or HTTP client.

Do not make such an object in the lifespan for the handlers. In a handler, it fails, for example with `RuntimeError: ... got Future <Future pending> attached to a different loop`.

Make one object for each worker loop when a handler first needs it. Each worker loop lives as long as the process, so each client lives as long as the process too:

```python title="news/clients.py"
import asyncio
import weakref

import httpx

_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = weakref.WeakKeyDictionary()


def search_client() -> httpx.AsyncClient:
    """Return the search client of the running event loop."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = httpx.AsyncClient(base_url="https://search.example.com")
        _clients[loop] = client
    return client
```

```python title="news/api.py"
from .clients import search_client


@api.get("/search")
async def search(q: str) -> dict:
    response = await search_client().get("/articles", params={"q": q})
    return response.json()
```

Objects that do not belong to a loop are safe to make in the lifespan. Examples are a `dict`, a `set`, a sync client, and data that you load from the database.

## Run more than one lifespan

`runbolt` runs the lifespan of each API that it finds. The startup order is the discovery order: the `BOLT_API` setting, or else the project API, then the apps in `INSTALLED_APPS` order. Shutdown runs in the reverse order.

Take a `blog` app and a `shop` app, in this order in `INSTALLED_APPS`. The lifespan of each app prints its name at startup and at shutdown. The output is:

```text
blog: start
shop: start
shop: stop
blog: stop
```

An API that you mount with `api.mount()` brings its lifespan with it. The lifespan of the parent starts first, then the lifespans of the mounted APIs, in mount order. Shutdown runs in the reverse order. Each lifespan gets its own API as `app`:

```python
reports_api = BoltAPI(lifespan=reports_lifespan)
search_api = BoltAPI(lifespan=search_lifespan)

api = BoltAPI(lifespan=lifespan)
api.mount("/reports", reports_api)
api.mount("/search", search_api)
```

```text
api: start
reports: start
search: start
search: stop
reports: stop
api: stop
```

## Lifespan and processes

The lifespan runs once in each process that serves requests.

- `runbolt` runs it once.
- `runbolt --processes 4` runs it in each of the 4 worker processes. The parent process does not run it. Thus each process has its own copy of the state that the lifespan loads.
- A worker that `runbolt` starts again, for example with `--max-rss` or `--workers-lifetime`, runs the startup of its lifespan. The worker that it replaces runs the shutdown.
- `runbolt --dev` starts a new process after each code change. The new process runs the lifespan again.

With this lifespan and `--processes 2`, each worker prints its own lines:

```python
import os


@asynccontextmanager
async def lifespan(app: BoltAPI):
    print(f"startup pid={os.getpid()}")
    yield
    print(f"shutdown pid={os.getpid()}")
```

```text
startup pid=7874
startup pid=7875
  19:59:48 [bolt] Shutting down 2 workers...
shutdown pid=7875
shutdown pid=7874
```

Do not use the lifespan for work that must run once for the whole deployment, for example a data migration. Run such work before you start `runbolt`.

## Test a lifespan

`TestClient` runs the startup when its `with` block starts, and the shutdown when the block ends. The startup sees the rows that the test made before the block:

```python title="tests/test_lifespan.py"
import pytest
from django.contrib.auth.models import User

from blog.models import Article
from django_bolt.testing import TestClient
from news.api import api, published_titles


@pytest.mark.django_db
def test_title_cache():
    author = User.objects.create_user(username="ada")
    Article.objects.create(title="Hello", body="...", author=author, published=True)

    with TestClient(api) as client:
        response = client.get("/titles/check", params={"title": "Hello"})
        assert response.json() == {"title": "Hello", "taken": True}

    assert published_titles == set()
```

The event loop of the `TestClient` lifespan does not run between startup and shutdown. Thus a background task of the lifespan does not run in such a test. See [Test the lifespan](testing.md#test-the-lifespan).

## See also

- [BoltAPI reference](../ref/api.md): the `lifespan` argument.
- [Testing](testing.md)
- [Deployment](deployment.md)
- [runbolt reference](../ref/runbolt.md)

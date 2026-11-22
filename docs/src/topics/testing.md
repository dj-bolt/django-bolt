---
description: Test Django-Bolt routes with TestClient. It sends each request through the request pipeline of runbolt, in the test process.
---

# Testing

`TestClient` sends requests to a `BoltAPI` in the test process. Each request goes through the same Rust pipeline as under `runbolt`: routing, validation, authentication, guards, middleware, CORS and compression. You start no server and open no port.

`TestClient` is an [`httpx.Client`](https://www.python-httpx.org/api/#client). Bolt depends on httpx, so you install nothing more.

```python title="tests/test_hello.py"
from django_bolt import BoltAPI
from django_bolt.testing import TestClient

api = BoltAPI()


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id, "title": "Hello"}


def test_get_article():
    with TestClient(api) as client:
        response = client.get("/articles/3")

    assert response.status_code == 200
    assert response.json() == {"id": 3, "title": "Hello"}
```

```bash
pytest tests/test_hello.py
```

```text
tests/test_hello.py .                                                    [100%]

============================== 1 passed in 0.15s ===============================
```

Always open the client with `with`. The `with` block runs the [lifespan](lifespan.md) of the API. It also lends the database connection of the test to the handlers, and releases the client at the end.

The test needs the pytest setup of the next section.

## Set up pytest

Install pytest and pytest-django:

```bash
pip install pytest pytest-django
```

Tell pytest-django where your settings are:

```toml title="pyproject.toml"
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "mysite.settings"
```

Import the API of your project in a fixture. The tests then run the routes, middleware and authentication that production runs. A route that you forgot to register fails a test, not a deploy.

```python title="tests/conftest.py"
import pytest

from blog.api import api
from django_bolt.testing import TestClient


@pytest.fixture
def client():
    with TestClient(api) as client:
        yield client
```

The other examples on this page test this app:

```python title="blog/models.py"
from django.conf import settings
from django.db import models


class Article(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
```

```python title="blog/api.py"
import msgspec

from django_bolt import BoltAPI, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.exceptions import NotFound

from .models import Article

api = BoltAPI()


class ArticleOut(msgspec.Struct):
    id: int
    title: str
    published: bool


class ArticleIn(msgspec.Struct):
    title: str
    body: str


@api.get("/articles")
async def list_articles() -> list[ArticleOut]:
    return [
        ArticleOut(id=a.id, title=a.title, published=a.published)
        async for a in Article.objects.filter(published=True).order_by("id")
    ]


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> ArticleOut:
    article = await Article.objects.filter(id=article_id).afirst()
    if article is None:
        raise NotFound(detail="Article not found")
    return ArticleOut(id=article.id, title=article.title, published=article.published)


@api.post("/articles", status_code=201, auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def create_article(request: Request, data: ArticleIn) -> ArticleOut:
    user = await request.auser()
    article = await Article.objects.acreate(title=data.title, body=data.body, author=user)
    return ArticleOut(id=article.id, title=article.title, published=article.published)
```

To test a reusable component, or to reproduce a bug alone, build a small API in the fixture:

```python title="tests/test_component.py"
import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


@pytest.fixture
def client():
    api = BoltAPI()

    @api.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    with TestClient(api) as client:
        yield client


def test_ping(client):
    assert client.get("/ping").json() == {"ok": True}
```

## Send requests

`TestClient` has one method for each HTTP method: `get`, `post`, `put`, `patch`, `delete`, `head`, `options` and `query`. The arguments are the arguments of httpx:

<!-- fragment -->
```python
client.get("/search", params={"q": "django", "page": 2})
client.get("/articles", headers={"Accept-Language": "fr"})
client.post("/articles", json={"title": "Hello", "body": "..."})
client.post("/login", data={"username": "ada", "password": "s3cret"})
client.post("/covers", files={"image": ("cover.png", image_bytes, "image/png")})
client.post("/import", content=b"raw bytes")
client.query("/articles/search", json={"title": "Hello", "body": "..."})
```

The response is an `httpx.Response`. Read `response.status_code`, `response.headers`, `response.json()`, `response.text` and `response.content`.

`TestClient` follows redirects. To check the redirect itself, pass `follow_redirects=False` to the request:

<!-- fragment -->
```python
response = client.get("/old-path", follow_redirects=False)
assert response.status_code == 307
assert response.headers["location"] == "/new-path"
```

## Test validation errors

Bolt validates the path, query, headers and body before the handler runs. Test the request that the handler gets, and also the request that it must not get:

```python title="tests/test_validation.py"
import msgspec

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: str
    body: str


@api.post("/articles", status_code=201)
async def create_article(data: ArticleIn) -> dict:
    return {"title": data.title}


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id}


def test_validation():
    with TestClient(api) as client:
        assert client.post("/articles", json={"title": "Hello", "body": "..."}).status_code == 201

        response = client.post("/articles", json={"body": "..."})
        assert response.status_code == 422
        assert response.json() == {
            "detail": [
                {"loc": ["body", "title"], "msg": "Object missing required field `title`", "type": "missing_field"}
            ]
        }

        response = client.get("/articles/abc")
        assert response.status_code == 422
        assert response.json() == {
            "detail": "Path parameter 'article_id': Invalid integer 'abc': invalid digit found in string"
        }

        # A path with no route is a 404.
        assert client.get("/nowhere").status_code == 404
```

## Test routes that use the database

Mark the test with `@pytest.mark.django_db`, as for each Django test. Create rows with the ORM, then send the request:

```python title="tests/test_articles.py"
import pytest
from django.contrib.auth.models import User

from blog.models import Article


@pytest.mark.django_db
def test_list_articles(client):
    author = User.objects.create_user(username="ada")
    hello = Article.objects.create(title="Hello", body="...", author=author, published=True)
    Article.objects.create(title="Draft", body="...", author=author, published=False)

    response = client.get("/articles")

    assert response.status_code == 200
    assert response.json() == [{"id": hello.id, "title": "Hello", "published": True}]
```

The test runs in a transaction that it does not commit. Handlers run on Bolt threads, and Django gives each thread its own connection. Thus, while the client is open, `TestClient` lends the connection of the test to the handler threads. The handlers read the rows of the test. The rows that a handler writes go into the same transaction, and the rollback at the end of the test removes them.

### Keep a cursor open in another thread

One connection serves one cursor at a time. The thread that sends a request gives the connection back while it waits, so a request inside a `QuerySet.iterator()` loop of the test works.

A second thread of the test that keeps a cursor open cannot give the connection back. The request then fails with `SharedTestConnectionError` after 10 seconds. Set the `DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT` environment variable to change the wait.

For such a test, pass `share_db_connection=False`. The handlers then use their own connections, so the test must commit its rows with `transaction=True`:

```python title="tests/test_export.py"
import threading

import pytest
from django.contrib.auth.models import User

from blog.api import api
from blog.models import Article
from django_bolt.testing import TestClient


@pytest.mark.django_db(transaction=True)
def test_export_runs_while_the_api_serves():
    author = User.objects.create_user(username="ada")
    for number in range(20):
        Article.objects.create(title=f"Article {number}", body="...", author=author, published=True)
    exported = []

    def export():
        for article in Article.objects.iterator():
            exported.append(article.title)

    with TestClient(api, share_db_connection=False) as client:
        worker = threading.Thread(target=export)
        worker.start()
        assert client.get("/articles").status_code == 200
        worker.join()

    assert len(exported) == 20
```

## Test authenticated routes

You do not need a login flow. `create_jwt_for_user()` signs a token with `SECRET_KEY`, the key that `JWTAuthentication` checks by default:

```python title="tests/test_auth.py"
import pytest
from django.contrib.auth.models import User

from blog.models import Article
from django_bolt.auth import create_jwt_for_user


@pytest.mark.django_db
def test_create_needs_a_token(client):
    response = client.post("/articles", json={"title": "Hello", "body": "..."})

    assert response.status_code == 401


@pytest.mark.django_db
def test_create_article(client):
    user = User.objects.create_user(username="ada", password="s3cret")
    token = create_jwt_for_user(user)

    response = client.post(
        "/articles",
        json={"title": "Hello", "body": "..."},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    assert Article.objects.get().author == user
```

`POST /articles` in `blog/api.py` has `auth=[JWTAuthentication()]` and `guards=[IsAuthenticated()]`. Keep the test with no token. It fails when somebody removes the guard.

For an API key, send the header that the backend reads, for example `headers={"X-API-Key": "..."}`. See [Authentication](authentication.md).

## Test streaming responses and SSE

`TestClient` reads the complete body of a streaming response. Assert on the body:

```python title="tests/test_events.py"
from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse
from django_bolt.testing import TestClient

api = BoltAPI()


@api.get("/articles/events", response_class=EventSourceResponse)
async def article_events():
    yield {"id": 1, "title": "Hello"}
    yield {"id": 2, "title": "Second post"}


def test_events():
    with TestClient(api) as client:
        response = client.get("/articles/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == 'data: {"id":1,"title":"Hello"}\n\ndata: {"id":2,"title":"Second post"}\n\n'


def test_events_line_by_line():
    with TestClient(api) as client:
        response = client.get("/articles/events", stream=True)

    data = [line for line in response.iter_lines() if line.startswith("data:")]
    assert data == ['data: {"id":1,"title":"Hello"}', 'data: {"id":2,"title":"Second post"}']
```

With `stream=True`, the response has `iter_content()` and `iter_lines()`. They split the body that the client already read. Thus a test with `TestClient` checks what the stream contains, not when each event arrives. To test the timing, use a [live server](#test-against-a-live-server).

Test a generator that never ends, such as a feed that sends an event each second, on a live server. `TestClient` waits for the end of the body.

## Test WebSocket routes

`WebSocketTestClient` opens one WebSocket connection to a route. It is async, so the test needs an event loop, for example from `pytest-asyncio`:

```python title="tests/test_comments.py"
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.testing import WebSocketTestClient

api = BoltAPI()


@api.websocket("/ws/comments")
async def comments(websocket: WebSocket):
    await websocket.accept()
    async for text in websocket.iter_text():
        await websocket.send_json({"comment": text})


@pytest.mark.asyncio
async def test_comments_socket():
    async with WebSocketTestClient(api, "/ws/comments") as ws:
        await ws.send_text("Nice article")
        assert await ws.receive_json() == {"comment": "Nice article"}
```

Bolt checks the path, authentication and guards of the connection in Rust, as on the server. See [Test a WebSocket route](websocket.md#test-a-websocket-route) for more cases, and the [testing reference](../ref/testing.md#websockettestclient) for each method.

## Change settings in a test

`TestClient` reads the Django settings when you create it, with the same code as `runbolt`. A test gets the upload limit, CORS, static files, media files and proxy trust of the server. Change a setting with `override_settings` before you create the client:

```python title="tests/test_upload.py"
from django.test import override_settings

from django_bolt import BoltAPI, Request
from django_bolt.testing import TestClient

api = BoltAPI()


@api.post("/import")
async def import_articles(request: Request) -> dict:
    return {"size": len(request.body)}


def test_default_limit():
    with TestClient(api) as client:
        assert client.post("/import", content=b"x" * (2 * 1024 * 1024)).status_code == 413


def test_larger_limit():
    with override_settings(BOLT_MAX_UPLOAD_SIZE=10 * 1024 * 1024), TestClient(api) as client:
        assert client.post("/import", content=b"x" * (2 * 1024 * 1024)).status_code == 200
```

Three arguments of `TestClient` replace settings for one client:

- `cors_allowed_origins=[...]` replaces the CORS origins. A `"*"` entry allows each origin.
- `static_files_config={...}` replaces the static files settings.
- `read_django_settings=False` uses no CORS, static files or media settings.

Bolt reads `BOLT_EMIT_SIGNALS` when you create the `BoltAPI`, not the client. See [Signals](signals.md).

## Test the lifespan

`TestClient` runs the startup part of the lifespan when the `with` block starts. It runs the shutdown part when the block ends. Each `with` block runs the lifespan again, so no state goes from one test to the next.

`TestClient` runs the lifespan on its own event loop. That loop does not run while the test sends requests. Thus a task that the lifespan starts with `asyncio.create_task()` does not run in a `TestClient` test. Use `AsyncTestClient` for such a lifespan: its lifespan runs on the loop of the test.

## Use AsyncTestClient

Use `AsyncTestClient` when the test is a coroutine, for example to await other work next to the request:

```python title="tests/test_async.py"
import pytest

from django_bolt import BoltAPI
from django_bolt.testing import AsyncTestClient

api = BoltAPI()


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id}


@pytest.mark.asyncio
async def test_get_article():
    async with AsyncTestClient(api) as client:
        response = await client.get("/articles/3")

    assert response.json() == {"id": 3}
```

`AsyncTestClient` does not lend the database connection of the test. Code in an event loop gets its own connection from Django. For an async test whose handlers use the database, commit the rows with `@pytest.mark.django_db(transaction=True)`.

## Test the OpenAPI schema

`TestClient` does not serve the `/docs` routes. `runbolt` adds them when it starts. To check the schema in a test, generate it. See [Check the schema in CI](openapi.md#check-the-schema-in-ci).

## Using Django's test runner

`manage.py test` works with `TestClient`. Django's own test client (`self.client`) does not find Bolt routes. It looks in `ROOT_URLCONF`, and Bolt routes are in the Rust router. Thus `self.client.get("/articles")` gives 404.

Use `TestCase` as the base class, also for handlers that use the database:

```python title="blog/tests.py"
from django.contrib.auth.models import User
from django.test import TestCase

from django_bolt.testing import TestClient

from .api import api
from .models import Article


class ArticleTests(TestCase):
    def test_list_articles(self):
        author = User.objects.create_user(username="ada")
        article = Article.objects.create(title="Hello", body="...", author=author, published=True)

        with TestClient(api) as client:
            response = client.get("/articles")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"id": article.id, "title": "Hello", "published": True}])
```

```bash
python manage.py test blog
```

```text
Found 1 test(s).
Creating test database for alias 'default'...
System check identified no issues (0 silenced).
.
----------------------------------------------------------------------
Ran 1 test in 0.020s

OK
Destroying test database for alias 'default'...
```

Use `TransactionTestCase` only for a test that passes `share_db_connection=False`. In a `TestCase`, such a client cannot see the rows of the test, and on SQLite the handler gets `database table is locked`. `TestClient` then gives a `BoltTestClientWarning` one time for each process.

## Test against a live server

Use a live server only for behavior that needs a real socket. For example, a client SDK that needs a URL, or an SSE consumer that must get events while the handler still sends them. Start `runbolt` in a subprocess and send requests with httpx:

```python title="tests/conftest.py"
import socket
import subprocess
import sys
import time

import httpx
import pytest


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def bolt_server():
    port = free_port()
    process = subprocess.Popen([sys.executable, "manage.py", "runbolt", "--host", "127.0.0.1", "--port", str(port)])
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}")
    try:
        deadline = time.time() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("runbolt exited before it became ready")
            try:
                client.get("/")  # Each response, also a 404, shows that the server is up.
                break
            except httpx.TransportError as err:
                if time.time() > deadline:
                    raise TimeoutError("server did not become ready in 15s") from err
                time.sleep(0.2)
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)
```

A test can then read a stream while the handler sends it. This route sends an event each second and never ends:

```python title="blog/api.py"
import asyncio

from django.utils import timezone

from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse

api = BoltAPI()


@api.get("/articles/feed", response_class=EventSourceResponse)
async def article_feed():
    while True:
        yield {"time": timezone.now().isoformat()}
        await asyncio.sleep(1)
```

```python title="tests/test_live.py"
def test_events_arrive_over_a_real_socket(bolt_server):
    with bolt_server.stream("GET", "/articles/feed") as response:
        assert response.status_code == 200
        first = next(line for line in response.iter_lines() if line.startswith("data:"))
        assert first.startswith('data: {"time":')
```

- Run pytest from the directory of `manage.py`, or pass `cwd=` to `Popen`.
- The server uses the database of your settings, not the test database. It does not see the rows that a test makes in its transaction.
- pytest-django already has a fixture with the name `live_server`. Give your fixture a different name.
- Each module that uses the fixture starts a server. Keep these tests few.

## See also

- [Testing reference](../ref/testing.md): each argument and method of the test clients.
- [Tutorial part 4: tests and running in production](../tutorial/part-4.md)
- [Lifespan](lifespan.md)
- [WebSocket](websocket.md)
- [Authentication](authentication.md)

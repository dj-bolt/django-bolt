---
icon: lucide/flask-conical
---

# Testing

Django-Bolt routes every request through its Rust HTTP layer, and so does its
test client. When a test passes against `TestClient`, the request went through
the same routing, parameter validation, authentication, and serialization code
that will handle it in production. There is no separate "test mode" that
quietly behaves differently.

`TestClient` runs entirely in-process — no server to start, no port to pick —
and it's built on `httpx`, which django-bolt already depends on, so there's
nothing extra to install:

```python
from django_bolt import BoltAPI
from django_bolt.testing import TestClient

api = BoltAPI()

@api.get("/hello")
async def hello():
    return {"message": "world"}

with TestClient(api) as client:
    response = client.get("/hello")
    assert response.status_code == 200
    assert response.json() == {"message": "world"}
```

That's the whole idea. The rest of this page covers the details: making
requests, working with the database, testing auth, and the rare cases where an
in-process client isn't enough.

## Making requests

If you've used `httpx` or `requests`, you already know this API. Each HTTP
method has its own function; query strings go in the URL, and headers, JSON
bodies, and form data are keyword arguments:

```python
with TestClient(api) as client:
    client.get("/search?q=test&limit=20")
    client.get("/secure", headers={"Authorization": "Bearer token"})
    client.post("/users", json={"name": "John", "email": "john@example.com"})
    client.post("/login", data={"username": "john", "password": "secret"})
    client.put("/users/1", json={"name": "Updated", "email": "u@example.com"})
    client.patch("/users/1", json={"name": "Patched"})
    client.delete("/users/1")
    client.query("/search", json={"term": "django"})
```

Responses give you the status code, headers, and body in whatever form you
need:

```python
response = client.get("/users/1")

response.status_code                    # 200
response.json()                         # decoded body
response.content                        # raw bytes
response.headers.get("content-type")   # "application/json"
```

## Testing validation

Path parameters, query parameters, headers, and request bodies are all
extracted and validated before your handler runs. A good test suite covers
both directions: the request that reaches the handler, and the request that
never should.

```python
import msgspec

class UserCreate(msgspec.Struct):
    name: str
    email: str

@api.post("/teams/{team_id}/users", status_code=201)
async def create_user(team_id: int, user: UserCreate, notify: bool = False):
    return {"team": team_id, "name": user.name, "notify": notify}
```

```python
with TestClient(api) as client:
    # The handler receives coerced, validated arguments
    response = client.post(
        "/teams/7/users?notify=true",
        json={"name": "John", "email": "john@example.com"},
    )
    assert response.status_code == 201
    assert response.json() == {"team": 7, "name": "John", "notify": True}

    # A body that fails validation never reaches the handler
    response = client.post("/teams/7/users", json={})
    assert response.status_code == 422

    # An unknown path is a plain 404
    assert client.get("/nonexistent").status_code == 404
```

Anything else a handler can declare — headers, cookies, form fields,
dependencies — is tested the same way: send the request, assert on the
response. For example, a header parameter:

```python
from typing import Annotated
from django_bolt.param_functions import Header

@api.get("/with-header")
async def with_header(x_custom: Annotated[str, Header()]):
    return {"header_value": x_custom}

with TestClient(api) as client:
    response = client.get("/with-header", headers={"X-Custom": "test-value"})
    assert response.json() == {"header_value": "test-value"}
```

## Streaming responses

Streaming endpoints work in tests too. By default the client buffers the whole
response, which is usually what you want — the stream has a beginning and an
end, and you assert on the result:

```python
from django_bolt.responses import EventSourceResponse

@api.get("/sse", response_class=EventSourceResponse)
async def sse():
    yield {"message": "hello"}
    yield {"message": "world"}

with TestClient(api) as client:
    response = client.get("/sse")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.content.decode()
    assert 'data: {"message":"hello"}' in body
    assert 'data: {"message":"world"}' in body
```

Pass `stream=True` to iterate over the body in chunks or lines instead:

```python
@api.get("/stream")
async def stream():
    async def generate():
        for i in range(5):
            yield f"data: {i}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")

with TestClient(api) as client:
    response = client.get("/stream", stream=True)

    lines = list(response.iter_lines())
    data_lines = [line for line in lines if line.startswith("data:")]
    assert len(data_lines) == 5
```

One thing to be aware of: even with `stream=True`, the in-process client
collects the response before handing it to you. You're testing what the stream
*contains*, not that events arrive while the handler is still producing them.
If the timing itself is the behavior under test, see
[testing against a live server](#testing-against-a-live-server) below.

WebSocket endpoints have their own test client — see
[Testing WebSockets](websocket.md#testing-websockets).

## Using pytest

Wrap the client in a fixture so tests stay short. Prefer importing the API
object from your real app module — that way your tests exercise the same
routes, middleware, and auth configuration that production runs, and a route
you forgot to register fails a test instead of a deploy:

```python
# tests/test_api.py
import pytest
from django_bolt.testing import TestClient
from myproject.api import api  # your actual API module

@pytest.fixture
def client():
    with TestClient(api) as client:
        yield client

def test_hello(client):
    response = client.get("/hello")
    assert response.status_code == 200
```

Sometimes you want a throwaway API instead — when testing a reusable
component, or reproducing a bug in isolation. Building one inside a fixture
works fine:

```python
@pytest.fixture
def api():
    api = BoltAPI()

    @api.get("/hello")
    async def hello():
        return {"message": "world"}

    return api

@pytest.fixture
def client(api):
    with TestClient(api) as client:
        yield client
```

## Endpoints that use the database

Mark the test with `@pytest.mark.django_db`, the same as any other Django test:

```python
import pytest
from django_bolt import BoltAPI
from django_bolt.testing import TestClient
from myapp.models import User

@pytest.fixture
def api():
    api = BoltAPI()

    @api.get("/users/{user_id}")
    async def get_user(user_id: int):
        user = await User.objects.aget(id=user_id)
        return {"id": user.id, "username": user.username}

    return api

@pytest.mark.django_db
def test_get_user(api):
    user = User.objects.create(username="testuser", email="test@example.com")

    with TestClient(api) as client:
        response = client.get(f"/users/{user.id}")
        assert response.status_code == 200
        assert response.json()["username"] == "testuser"
```

Rollback removes the rows after the test, thus you do not have to delete them
yourself, and no marker option is necessary.

### How a handler reads rows the test did not commit

`TestClient` sends requests through the Rust pipeline, thus your handlers run
on framework threads. Django gives each thread its own database connection. The
row that `User.objects.create()` made is in the transaction of the test and is
not committed, thus a second connection cannot read it:

```text
Test thread (connection A)          Handler thread (connection B)
──────────────────────────          ─────────────────────────────
BEGIN TRANSACTION
User.objects.create(id=1)
  ↓ (not committed)
                                    TestClient.get("/users/1")
                                      ↓
                                    await User.objects.aget(id=1)
                                      ↓
                                    ❌ not found
ROLLBACK
```

While the client is open, `TestClient` lends the connection of the test to the
handler threads. They are then in the same transaction, thus they read the same
rows:

```text
Test thread                         Handler thread
───────────                         ──────────────
BEGIN TRANSACTION
User.objects.create(id=1)
  ↓ (not committed)
                                    TestClient.get("/users/1")
                                      ↓ same connection
                                    await User.objects.aget(id=1)
                                      ↓
                                    ✅ found
ROLLBACK — the row is removed
```

This is the mechanism Django uses for its own live-server tests. A row that a
handler writes goes into the transaction of the test in the same way. Thus the
test can read it, and rollback removes it.

### When to switch the sharing off

One connection cannot serve two threads at the same moment. `TestClient` puts a
lock on the shared connection. Thus database work from the test and from a
handler waits its turn, in place of corrupting rows.

A request inside a `QuerySet.iterator()` loop is safe. The thread that makes the
request waits in the Rust pipeline and runs no query, thus it gives its lock
back for the time of the request and takes it again after:

```python
for user in User.objects.iterator():     # holds a cursor open
    client.get(f"/users/{user.id}")      # safe: this thread parks its lock
```

A second thread of the test cannot be parked in that way, because it is not
waiting for the response. If such a thread holds a cursor open, the handler
waits for it, and Bolt raises `SharedTestConnectionError` in place of a 500 with
no cause. Read the rows in that thread first:

```python
for user in list(User.objects.all()):
    ...
```

Or switch the sharing off and commit the rows instead:

```python
@pytest.mark.django_db(transaction=True)
def test_background_thread(api):
    with TestClient(api, share_db_connection=False) as client:
        ...
```

With `share_db_connection=False` the handler threads use their own connections
again, thus the test must commit its rows: use
`@pytest.mark.django_db(transaction=True)`, or `TransactionTestCase`. Those
commit each row, and the tables are flushed after the test.

Two tests must not enter a `TestClient` in two threads at the same time. The
connection store of Django is process-wide, thus the second client would lend
the connection of the first test to its own handlers. Bolt refuses this and
names the fixes. No test runner does it: pytest runs the tests one after the
other, and pytest-xdist and `manage.py test --parallel` each use a separate
process.

The lock has a limit of 10 seconds. Set `DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT` to
change it.

### Or create data through the API

For end-to-end flows, skip the ORM in the test entirely and create data
through the API itself:

```python
@pytest.mark.django_db
def test_create_and_get_user():
    with TestClient(api) as client:
        create_response = client.post(
            "/users",
            json={"username": "testuser", "email": "test@example.com"},
        )
        assert create_response.status_code == 200
        user_id = create_response.json()["id"]

        get_response = client.get(f"/users/{user_id}")
        assert get_response.status_code == 200
        assert get_response.json()["username"] == "testuser"
```

Reaching for the ORM is more direct when a test needs specific data in place;
going through the API tests the full round trip. Use whichever reads better
for the test at hand.

### ViewSets

Class-based views need nothing special — register the viewset and hit its
routes:

```python
import pytest
from django_bolt import BoltAPI, ViewSet
from django_bolt.testing import TestClient
from myapp.models import Article

@pytest.fixture
def api():
    api = BoltAPI()

    @api.viewset("/articles")
    class ArticleViewSet(ViewSet):
        queryset = Article.objects.all()

        async def list(self, request) -> list[dict]:
            articles = []
            async for article in await self.get_queryset():
                articles.append({"id": article.id, "title": article.title})
            return articles

        async def retrieve(self, request) -> dict:
            article = await self.get_object()
            return {"id": article.id, "title": article.title}

    return api

@pytest.mark.django_db
def test_article_viewset(api):
    Article.objects.create(title="Test Article", content="Content")

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200
        assert len(response.json()) == 1

        response = client.get("/articles/1")
        assert response.status_code == 200
        assert response.json()["title"] == "Test Article"
```

## Using Django's test runner

The sections above use pytest. The `manage.py test` runner also works, but you
must make one change.

**Django's test client does not find Bolt routes.** `self.client` looks for the
route in `ROOT_URLCONF`. Bolt handlers are in the Rust router, not in the Django
URL configuration. Thus a request to a route that exists gives a 404:

```python
from django.test import TestCase


class Wrong(TestCase):
    def test_slides(self):
        response = self.client.get("/slides/")
        assert response.status_code == 404  # not registered in ROOT_URLCONF
```

Use `TestClient` in its place. `TestClient` sends the request through the Rust
pipeline. Thus routing, middleware, and authentication operate as they do in
production.

`TestCase` is correct for handlers that use the database, because `TestClient`
lends the connection of the test to the handler threads. No special base class
and no marker are necessary:

```python
from django.test import TestCase
from django_bolt.testing import TestClient

from .api import api
from .models import Slide


class TestSlides(TestCase):
    def test_lists_slides(self):
        slide = Slide.objects.create(description="hello")

        with TestClient(api) as client:
            response = client.get("/slides/")
            assert response.status_code == 200
            assert response.json() == [{"id": slide.pk, "description": "hello"}]
```

Use `TransactionTestCase` for a test that switches the sharing off with
`TestClient(api, share_db_connection=False)`. See
[When to switch the sharing off](#when-to-switch-the-sharing-off). Such a test
gets a `BoltTestClientWarning` if it stays on `TestCase`, because the handler
then cannot read its rows. To stop the warning, use this filter:

```python
import warnings

from django_bolt.testing import BoltTestClientWarning

warnings.simplefilter("ignore", BoltTestClientWarning)
```

## Authenticated endpoints

You don't need a login flow to test a protected route.
`create_jwt_for_user` signs a token with your `SECRET_KEY` — the same default
`JWTAuthentication` verifies against — so a real user plus one function call
gets you valid credentials. Token validation happens in Rust from the token's
claims; no database lookup runs per request.

```python
import pytest
from django.contrib.auth.models import User
from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user
from django_bolt.testing import TestClient

api = BoltAPI()

@api.get("/private", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def private():
    return {"ok": True}


@pytest.mark.django_db
def test_private_requires_a_token():
    user = User.objects.create_user(username="alice", password="s3cret")
    token = create_jwt_for_user(user)

    with TestClient(api) as client:
        # No token: the guard rejects the request before the handler runs
        assert client.get("/private").status_code == 401

        response = client.get("/private", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
```

Test the locked door, not just the open one: the 401 assertion is the one
that catches a route someone forgot to guard.

## Lifespan events

If your API defines a [lifespan](lifespan.md), `TestClient` runs it: startup
when you enter the `with` block, shutdown when you leave. Each `with` block is
its own fresh instance, so state never leaks from one test to the next.

```python
from contextlib import asynccontextmanager
from django_bolt import BoltAPI
from django_bolt.testing import TestClient

@asynccontextmanager
async def lifespan(app):
    app._cache = {"ready": True}
    yield
    app._cache.clear()

api = BoltAPI(lifespan=lifespan)

@api.get("/status")
async def status(request):
    return {"ready": request.app._cache.get("ready", False)}

with TestClient(api) as client:
    # startup has already run
    response = client.get("/status")
    assert response.json() == {"ready": True}
# shutdown runs here
```

## AsyncTestClient

When the test itself is async — because it awaits other things alongside the
request, or your suite runs under `pytest-asyncio` — use `AsyncTestClient`.
Same API, awaitable:

```python
import pytest
from django_bolt import BoltAPI
from django_bolt.testing import AsyncTestClient

api = BoltAPI()

@api.get("/hello")
async def hello():
    return {"message": "world"}

@pytest.mark.asyncio
async def test_async():
    async with AsyncTestClient(api) as client:
        response = await client.get("/hello")
        assert response.status_code == 200
        assert response.json() == {"message": "world"}
```

`AsyncTestClient` does not share the connection of the test. Django keeps its
connections in a thread-critical `asgiref` Local, thus code in an event loop
gets a different connection object and cannot join the transaction of the test.
For an async test whose handlers use the database, commit the rows with
`@pytest.mark.django_db(transaction=True)`.

## Testing against a live server

Almost everything belongs in `TestClient`. But it runs in-process, and once in
a while that's the wrong altitude: a client SDK that insists on a real URL, an
SSE consumer that must receive events *while* the handler is producing them,
or a smoke test that your project actually boots under `runbolt` the way it
will in production.

For those cases you don't need anything from django-bolt — start your own
project's server in a subprocess and point `httpx` at it:

```python
# conftest.py
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


@pytest.fixture(scope="session")
def bolt_server():
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "manage.py", "runbolt", "--host", "127.0.0.1", "--port", str(port)]
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}")
    try:
        deadline = time.time() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("runbolt exited before it became ready")
            try:
                client.get("/health")  # any route works: a response means it's up
                break
            except httpx.TransportError:
                if time.time() > deadline:
                    raise TimeoutError("server did not become ready in 15s")
                time.sleep(0.2)
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)
```

Don't name the fixture `live_server` — pytest-django reserves that name for
its own fixture, and if you shadow it, pytest-django will skip your tests
whenever `DJANGO_SETTINGS_MODULE` isn't configured, silently.

Now a test can consume a stream as it arrives, over a real socket:

```python
def test_events_arrive_while_streaming(bolt_server):
    with bolt_server.stream("GET", "/sse") as response:
        first_event = next(
            line for line in response.iter_lines() if line.startswith("data:")
        )
        assert first_event == 'data: {"message":"hello"}'
```

Keep these tests rare. Starting a server costs around a second per test —
hundreds of times slower than `TestClient` — and everything about your
handlers' behavior is already covered in-process. Reserve the live server for
behavior that only exists on a real socket.

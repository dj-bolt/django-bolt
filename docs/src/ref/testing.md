---
description: Reference for django_bolt.testing - TestClient, AsyncTestClient, WebSocketTestClient and their errors and warnings.
---

# Testing reference

The `django_bolt.testing` module has the test clients of Django-Bolt. For a task-based guide, see [Testing](../topics/testing.md).

<!-- fragment -->
```python
from django_bolt.testing import (
    AsyncTestClient,
    BoltTestClientWarning,
    ConnectionClosed,
    HandshakeRejected,
    SharedTestConnectionError,
    TestClient,
    WebSocketTestClient,
)
```

## TestClient

<!-- fragment -->
```python
TestClient(
    api: BoltAPI,
    base_url: str = "http://testserver",
    raise_server_exceptions: bool = True,
    cors_allowed_origins: list[str] | None = None,
    read_django_settings: bool = True,
    use_http_layer: bool = True,
    static_files_config: dict | None = None,
    share_db_connection: bool = True,
    **kwargs,
)
```

A synchronous HTTP client for one `BoltAPI`. It is a subclass of `httpx.Client`. Each request runs on a Bolt test worker thread, through the request pipeline of `runbolt`. The calling thread waits for the response.

The constructor registers the HTTP routes, WebSocket routes, ASGI mounts, MCP mounts and middleware of `api`. It also registers the OpenAPI docs routes, with the code of `runbolt`. The `auth`, `guards` and `enabled` values of the `OpenAPIConfig` of `api` apply. A route of `api` with the path of a docs route raises `ValueError`, as `runbolt` refuses to start with it. The client does not add the docs routes to `api`, so a later `mount()` of `api` does not copy them. The schema has the routes of `api` at the creation of the client.

### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `api` | `BoltAPI` | required | The API to test. |
| `base_url` | `str` | `"http://testserver"` | The base URL of each request. The host of this URL is the `Host` header. |
| `raise_server_exceptions` | `bool` | `True` | If `True`, an exception that gives a 5xx response raises in the test. This includes an exception of a handler, of a streaming body and of the test backend. An exception that the API answers with a response, such as an `HTTPException` or a 4xx, gives that response. If `False`, each such exception gives the 500 response of `runbolt`. See [Exceptions in handlers](../topics/testing.md#exceptions-in-handlers). |
| `cors_allowed_origins` | `list[str] \| None` | `None` | Replaces the CORS origins of the settings. A `"*"` entry allows each origin. The other CORS values are the defaults of the server. |
| `read_django_settings` | `bool` | `True` | If `True`, read the CORS, static files and media settings, as `runbolt` does. If `False`, use none of them. |
| `use_http_layer` | `bool` | `True` | Ignored. Each request goes through the HTTP layer. |
| `static_files_config` | `dict \| None` | `None` | Replaces the static files settings. The keys are `url_prefix` (default `"/static"`), `directories` (a list of paths), `csp_header` and `cache_control`. |
| `share_db_connection` | `bool` | `True` | If `True`, the handler threads use the database connection of the test while the client is open, when that connection is in a transaction. See [Test routes that use the database](../topics/testing.md#test-routes-that-use-the-database). |
| `follow_redirects` | `bool` | `True` | If `True`, follow redirects. If `False`, give the redirect response. A request can also set it. |
| `**kwargs` | | | Passed to `httpx.Client`. |

Bolt reads the other settings when you create the client, with the code of `runbolt`. Examples are `BOLT_MAX_UPLOAD_SIZE`, `BOLT_TRUSTED_PROXIES` and `BOLT_COMPRESSION`.

### Context manager

Use the client in a `with` block. Entering the block:

1. Lends the database connection of the test to the handler threads, if `share_db_connection` is `True` and the connection is in a transaction.
2. Runs the startup part of the [lifespan](../topics/lifespan.md) of `api` and of the APIs that it mounts.

Leaving the block runs the lifespan shutdown, stops the lending, releases the test app, and closes the database connections of the test worker threads.

`close()` does the same steps for a client that you entered without `with`.

The lifespan runs on an event loop of the client. That loop runs only during startup and shutdown.

### Methods

`get`, `post`, `put`, `patch`, `delete`, `head` and `options` take the arguments of the `httpx.Client` method with the same name, and one more keyword argument:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `stream` | `bool` | `False` | If `True`, add `iter_content()` and `iter_lines()` to the response. |

`query(url, *, stream=False, **kwargs)` sends a request with the `QUERY` method. It takes the arguments of `httpx.Client.request()`.

`request()`, `send()` and `stream()` are the methods of `httpx.Client`.

Each method returns an `httpx.Response`. By default, the client follows redirects. Pass `follow_redirects=False` to the client or to a request to get the redirect response.

### Streaming responses

`TestClient` reads the complete body before it returns the response, also with `stream=True`. A streaming response that never ends blocks the request.

With `stream=True`, the response has two more methods:

| Method | Description |
| --- | --- |
| `iter_content(chunk_size=1024, decode_unicode=False)` | Yield the body in parts of `chunk_size` bytes. With `decode_unicode=True`, yield UTF-8 text. |
| `iter_lines(decode_unicode=True)` | Yield the body one line at a time. Bolt splits the body at `\n` and removes it. A `\r` before it stays. With `decode_unicode=False`, yield `bytes`. |

### Errors

| Error | When |
| --- | --- |
| `RuntimeError("TestClient is closed. Create a new TestClient for each `with` block.")` | You enter a client that you already closed. |
| `RuntimeError("TestClient is already open. Enter it once.")` | You enter a client two times. |
| `SharedTestConnectionError` | Two threads need the lent connection at the same time, and neither can wait. See [SharedTestConnectionError](#sharedtestconnectionerror). |
| `TypeError` | A configuration decorator, for example a rate limit, is above the route decorator of a handler. |
| The startup errors of `runbolt` | Two routes conflict, or a route conflicts with an ASGI or MCP mount. |

### Example

```python
import django
from django.conf import settings

settings.configure(SECRET_KEY="x" * 50, INSTALLED_APPS=["django_bolt"])
django.setup()

from django_bolt import BoltAPI  # noqa: E402
from django_bolt.testing import TestClient  # noqa: E402

api = BoltAPI()


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id}


with TestClient(api) as client:
    response = client.get("/articles/3")
    print(response.status_code, response.json())
```

```text
200 {'id': 3}
```

## AsyncTestClient

<!-- fragment -->
```python
AsyncTestClient(
    api: BoltAPI,
    base_url: str = "http://testserver",
    raise_server_exceptions: bool = True,
    cors_allowed_origins: list[str] | None = None,
    read_django_settings: bool = True,
    static_files_config: dict | None = None,
    **kwargs,
)
```

An async HTTP client for one `BoltAPI`. It is a subclass of `httpx.AsyncClient`. The parameters are the same as for [`TestClient`](#testclient), with these differences:

- It has no `share_db_connection` parameter. It never lends the database connection of the test. Code in an event loop gets its own connection from Django, so a test that uses the database needs committed rows.
- It has no `use_http_layer` parameter.
- Its methods are the methods of `httpx.AsyncClient`. They have no `stream` argument, and the client has no `query()` method. Use `request("QUERY", url)`.

Use it with `async with`. The lifespan of the API runs on the event loop of the test, so tasks that the lifespan starts run during the test.

```python
import asyncio

import django
from django.conf import settings

settings.configure(SECRET_KEY="x" * 50, INSTALLED_APPS=["django_bolt"])
django.setup()

from django_bolt import BoltAPI  # noqa: E402
from django_bolt.testing import AsyncTestClient  # noqa: E402

api = BoltAPI()


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    return {"id": article_id}


async def main():
    async with AsyncTestClient(api) as client:
        response = await client.get("/articles/3")
        print(response.status_code, response.json())


asyncio.run(main())
```

```text
200 {'id': 3}
```

Errors: `RuntimeError("AsyncTestClient is closed. Create a new AsyncTestClient for each `async with` block.")` and `RuntimeError("AsyncTestClient is already open. Enter it once.")`, as for `TestClient`.

## WebSocketTestClient

<!-- fragment -->
```python
WebSocketTestClient(
    api: BoltAPI,
    path: str,
    headers: dict[str, str] | None = None,
    query_string: str = "",
    subprotocols: list[str] | None = None,
    cors_allowed_origins: list[str] | None = None,
    read_django_settings: bool = True,
)
```

An async client for one WebSocket connection. Rust matches the path, and checks the authentication, the guards and the origin of the connection, as on the server. Then the client runs the handler in the event loop of the test.

### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `api` | `BoltAPI` | required | The API that has the WebSocket route. |
| `path` | `str` | required | The path of the route, for example `"/ws/comments"`. |
| `headers` | `dict[str, str] \| None` | `None` | The headers of the handshake. To authenticate, send the credential that the backend reads, for example `{"Authorization": "Bearer <token>"}`. |
| `query_string` | `str` | `""` | The query string, with no `?`. |
| `subprotocols` | `list[str] \| None` | `None` | The subprotocols to ask for. |
| `cors_allowed_origins` | `list[str] \| None` | `None` | Replaces the CORS origins of the settings for the origin check. |
| `read_django_settings` | `bool` | `True` | If `True`, read the CORS settings when `cors_allowed_origins` is `None`. |

### Context manager

`async with WebSocketTestClient(...) as ws` connects and starts the handler. The entry waits until the handler calls `accept()` or refuses the connection.

Leaving the block sends a disconnect to the handler. The client waits 1 second for the handler to end, then cancels it. If the handler raised an error after `accept()`, the exit raises that error again.

### Methods

| Method | Description |
| --- | --- |
| `await send_text(data: str)` | Send a text message. |
| `await send_bytes(data: bytes)` | Send a binary message. |
| `await send_json(data, mode="text")` | Encode `data` as JSON and send it. With `mode="binary"`, send it as bytes. |
| `await receive(timeout=5.0)` | Return the next raw ASGI message, a `dict`. |
| `await receive_text(timeout=5.0)` | Return the next message as `str`. |
| `await receive_bytes(timeout=5.0)` | Return the next message as `bytes`. |
| `await receive_json(timeout=5.0, mode="text")` | Return the next message, decoded from JSON. |
| `await close(code=1000)` | Close the connection from the client side. |
| `iter_text(timeout=5.0)` | An async iterator of text messages. It stops at a close or a timeout. |
| `iter_bytes(timeout=5.0)` | An async iterator of binary messages. It stops at a close or a timeout. |
| `iter_json(timeout=5.0)` | An async iterator of JSON messages. It stops at a close or a timeout. |

### Properties

| Property | Type | Description |
| --- | --- | --- |
| `accepted` | `bool` | `True` after the handler accepted the connection. |
| `closed` | `bool` | `True` after a close from either side. |
| `close_code` | `int \| None` | The close code, after a close. |
| `accepted_subprotocol` | `str \| None` | The subprotocol that the handler accepted. |

### Errors

| Error | When |
| --- | --- |
| `ValueError("No WebSocket handler found for path: ...")` | No WebSocket route matches `path`. |
| `PermissionError("WebSocket connection denied: ...")` | Authentication, a guard or the origin check refused the connection, or the token is revoked. |
| `HandshakeRejected` | The handler returned, or raised `WebSocketException`, before `accept()`. |
| `TimeoutError` | The handler did not accept or refuse within 5 seconds. A `receive*()` call got no message within its `timeout`. |
| `ConnectionClosed` | A `receive*()` call found the connection closed. |
| `RuntimeError("WebSocket is closed")` | You send a message after a close. |
| The error of the handler | The handler raised an error before `accept()`. |

### Example

```python
import asyncio

import django
from django.conf import settings

settings.configure(SECRET_KEY="x" * 50, INSTALLED_APPS=["django_bolt"])
django.setup()

from django_bolt import BoltAPI, WebSocket  # noqa: E402
from django_bolt.testing import ConnectionClosed, WebSocketTestClient  # noqa: E402

api = BoltAPI()


@api.websocket("/ws/comments")
async def comments(websocket: WebSocket):
    await websocket.accept()
    text = await websocket.receive_text()
    await websocket.send_json({"comment": text})
    await websocket.close(reason="done")


async def main():
    async with WebSocketTestClient(api, "/ws/comments") as ws:
        await ws.send_text("Nice article")
        print(await ws.receive_json())
        try:
            await ws.receive_text()
        except ConnectionClosed as exc:
            print(exc.code, exc.reason)


asyncio.run(main())
```

```text
{'comment': 'Nice article'}
1000 done
```

## HandshakeRejected

<!-- fragment -->
```python
class HandshakeRejected(Exception):
    status_code: int
```

`WebSocketTestClient` raises it when the handler refuses the handshake. On a server, the client gets an HTTP response with `status_code` and no connection. A handler that returns or raises `WebSocketException` before `accept()` gives `403`.

## ConnectionClosed

<!-- fragment -->
```python
class ConnectionClosed(Exception):
    code: int
    reason: str
```

`WebSocketTestClient` raises it when a `receive*()` call finds the connection closed. `code` is the close code. `reason` is the close reason, or `""`.

## SharedTestConnectionError

A subclass of `RuntimeError`. `TestClient` raises it in the test thread in two cases:

- A second thread enters a `TestClient` while another thread lends its connection. Django keeps one store of connections for each process, so the rows of two tests would mix.
- Another thread of the test holds a cursor open, and a handler needs the lent connection. The handler waits 10 seconds, then the request fails. A cursor of the thread that sends the request does not cause this.

The message names the cause and the fixes. The fixes are: read the rows before the request, pass `share_db_connection=False` and commit the rows, or wait longer.

Set the wait in seconds with the `DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT` environment variable. The value must be a finite number above zero. Another value raises `ValueError` when you create the client.

## BoltTestClientWarning

A subclass of `RuntimeWarning`. `TestClient` gives it one time for each process, when a client that does not lend its connection starts inside a database transaction. Its handlers cannot read the rows of the test. On SQLite, they get `database table is locked`.

To fix it, remove `share_db_connection=False`, or commit the rows with `TransactionTestCase` or `django_db(transaction=True)`. To silence it:

<!-- fragment -->
```python
import warnings

from django_bolt.testing import BoltTestClientWarning

warnings.simplefilter("ignore", BoltTestClientWarning)
```

## See also

- [Testing](../topics/testing.md)
- [WebSocket](../topics/websocket.md)
- [Lifespan](../topics/lifespan.md)

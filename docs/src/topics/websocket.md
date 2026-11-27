---
description: Serve WebSocket connections with typed parameters, authentication, guards and close codes, and test them with WebSocketTestClient.
---

# WebSocket

A WebSocket is a connection that stays open, so the server and the client can send messages at any time.
Use it for chat, live comments or collaborative editing.
For updates that go only from the server to the client, [Server-Sent Events](sse.md) are simpler.

Register a WebSocket route with `@api.websocket()`.
The handler gets a `WebSocket` object:

```python title="blog/api.py"
from django_bolt import BoltAPI
from django_bolt.websocket import WebSocket

api = BoltAPI()


@api.websocket("/ws/articles/{article_id}/comments")
async def comment_room(websocket: WebSocket, article_id: int) -> None:
    await websocket.accept()
    async for comment in websocket.iter_json():
        await websocket.send_json({"article_id": article_id, "body": comment["body"]})
```

Connect from a browser:

```javascript
const socket = new WebSocket("ws://localhost:8000/ws/articles/7/comments");
socket.onmessage = (event) => console.log(JSON.parse(event.data));
socket.onopen = () => socket.send(JSON.stringify({ body: "Nice article" }));
```

The browser logs `{article_id: 7, body: "Nice article"}`.

A WebSocket handler must be an `async def` function.
`@api.websocket()` raises `TypeError` for a `def` function.

## Accept the connection

A connection starts with an HTTP request: the handshake.
Bolt runs the rate limit, the Origin check, the authentication and the guards before the handler starts.
The handler then decides the handshake:

- `await websocket.accept()` sends `101 Switching Protocols`, and the connection opens.
- A `close()` or a `return` before `accept()` refuses the handshake with `403`.

Call `accept()` before you send or receive messages.
A second call to `accept()` does nothing.

## Send and receive messages

| Method | Does |
| --- | --- |
| `await websocket.send_text(data)` | Sends a text frame. |
| `await websocket.send_bytes(data)` | Sends a binary frame. |
| `await websocket.send_json(data, mode="text")` | Encodes `data` as JSON and sends it. `mode="binary"` sends a binary frame. |
| `await websocket.receive_text()` | Waits for a text frame and returns a `str`. |
| `await websocket.receive_bytes()` | Waits for a binary frame and returns `bytes`. |
| `await websocket.receive_json(mode="text")` | Waits for a frame and decodes its JSON. |
| `websocket.iter_text()`, `iter_bytes()`, `iter_json()` | Give each message until the client disconnects. |
| `await websocket.receive()` | Returns the raw ASGI message, a `dict`. |

`receive_text()` raises `WebSocketException` with code 1003 for a binary frame.
`receive_bytes()` does the same for a text frame.
The connection stays open, so you can handle the error and continue:

```python
from django_bolt.websocket import WebSocketDisconnect, WebSocketException


@api.websocket("/ws/drafts/{draft_id}")
async def draft_editor(websocket: WebSocket, draft_id: int) -> None:
    await websocket.accept()
    while True:
        try:
            text = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except WebSocketException as exc:
            await websocket.send_text(f"Error {exc.code}: {exc.reason}")
            continue
        await websocket.send_text(f"Saved {len(text)} characters")
```

A binary frame gives the client the message `Error 1003: receive_text() got a binary frame`.
`WebSocketDisconnect` is a subclass of `WebSocketException`, so catch it first.

## Read parameters

A handler reads path, query, header and cookie parameters as an HTTP handler does.
The `WebSocket` parameter can have any name. Bolt finds it by its type hint.

```python
from typing import Annotated

from django_bolt.params import Cookie, Header, Query


@api.websocket("/ws/articles/{article_id}/presence")
async def presence(
    websocket: WebSocket,
    article_id: int,
    nickname: Annotated[str, Query()] = "guest",
    user_agent: Annotated[str, Header()] = "unknown",
    theme: Annotated[str, Cookie()] = "light",
) -> None:
    await websocket.accept()
    await websocket.send_json(
        {"article_id": article_id, "nickname": nickname, "user_agent": user_agent, "theme": theme}
    )
```

A request to `/ws/articles/7/presence?nickname=ada` with the cookie `theme=dark` gets:

```json
{"article_id": 7, "nickname": "ada", "user_agent": "Mozilla/5.0", "theme": "dark"}
```

A value that does not convert refuses the handshake with `400`.
For example, `/ws/articles/abc/presence` gets `400`, and the handler does not run.
A value that fails a check of its parameter gets `400` too, before `accept()`.
Examples are an item of a `list[int]`, a `Query(ge=1)` limit and a field of a parameter model.

A route matches the request path with and without a trailing slash.
Thus `/live` and `/live/` are one route, and a second route at that path stops the startup.
[Routes that stop the startup](routing.md#routes-that-stop-the-startup) gives each such error.
A WebSocket has no request body, so a body, form or file parameter is not available.

The `WebSocket` object also has these attributes: `path`, `path_info`, `query_params`, `headers`, `cookies`, `path_params`, `subprotocols`, `client` and `state`.
With `FORCE_SCRIPT_NAME`, `path` starts with the script name and `path_info` does not, as for a request. See [Serve under a sub-path](deployment.md#serve-under-a-sub-path).

### Use dependencies

`Depends()` works on a WebSocket route. A dependency can be `def` or `async def`:

```python
from django_bolt import Depends


async def room_settings(room: str = "general") -> dict:
    return {"room": room, "max_length": 500}


@api.websocket("/ws/chat")
async def chat(websocket: WebSocket, settings: dict = Depends(room_settings)) -> None:
    await websocket.accept()
    await websocket.send_json(settings)
```

`/ws/chat?room=django` sends `{"room": "django", "max_length": 500}`.
See [Dependencies](dependencies.md).

## Handle a disconnect

`receive_text()`, `receive_bytes()`, `receive_json()` and `receive()` raise `WebSocketDisconnect` when the client leaves.
Its `code` attribute has the close code of the client.
The `iter_*()` methods catch it and stop:

```python
from django_bolt.websocket import WebSocketDisconnect


@api.websocket("/ws/articles/{article_id}/readers")
async def readers(websocket: WebSocket, article_id: int) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect as exc:
        print(f"Reader of article {article_id} left with code {exc.code}")
```

A send that finds that the client left raises `WebSocketDisconnect`, with code 1006.
A send after Bolt knows of the disconnect, for example after `receive_text()` raised, raises `RuntimeError`.
The handler continues to run after the client leaves, so code after the receive loop runs.

## Close the connection

Call `close()` with a code and a reason:

```python
@api.websocket("/ws/articles/{article_id}/edit")
async def edit_article(websocket: WebSocket, article_id: int) -> None:
    await websocket.accept()
    await websocket.close(code=4001, reason="Read-only article")
```

The rules for a close frame come from RFC 6455:

- The code is 1000 to 1003, 1007 to 1014, or 3000 to 4999. Use 4000 to 4999 for codes of your application.
- The reason is at most 123 bytes of UTF-8.

`close()` raises `ValueError` for a code or a reason that breaks these rules.
The connection stays open.
A second `close()` does nothing.

You can also raise an exception to close the connection:

```python
from django_bolt.websocket import WebSocketClose


@api.websocket("/ws/articles/{article_id}/votes")
async def votes(websocket: WebSocket, article_id: int) -> None:
    await websocket.accept()
    raise WebSocketClose(code=4003, reason="Voting is closed")
```

`WebSocketClose` has the default code 1000.
`WebSocketException` has the default code 1011.
Both check the code and the reason as `close()` does.

| The handler after `accept()` | The client gets |
| --- | --- |
| Calls `close(code, reason)` | A close frame with `code` and `reason` |
| Raises `WebSocketClose` or `WebSocketException` | A close frame with the code and the reason of the exception |
| Returns | A close frame with code 1000 |
| Raises another exception | A close frame with code 1011 and the reason `Internal error`. Bolt prints the error to stderr. |

`CloseCode` has names for the standard codes, for example `CloseCode.NORMAL` (1000) and `CloseCode.POLICY_VIOLATION` (1008).

## Refuse the handshake

Decide before `accept()` if the connection can open:

```python
@api.websocket("/ws/drafts/{draft_id}/preview")
async def draft_preview(websocket: WebSocket, draft_id: int) -> None:
    if draft_id > 100:
        await websocket.close()
        return
    await websocket.accept()
    await websocket.send_text(f"Preview of draft {draft_id}")
```

```bash
curl -i -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  localhost:8000/ws/drafts/500/preview
```

```text
HTTP/1.1 403 Forbidden
content-type: application/json

{"detail":"WebSocket connection refused"}
```

| The handler before `accept()` | Response |
| --- | --- |
| Calls `close()` | `403 Forbidden` |
| Returns | `403 Forbidden` |
| Raises `WebSocketException` or `WebSocketClose` | `403 Forbidden` |
| Raises another exception | `500 Internal Server Error` |

## Check the connection state

`websocket.state` is a `WebSocketState`. The state only moves forward:
`CONNECTING`, then `CONNECTED` after `accept()`, then `DISCONNECTED` after a close.

| Call | `CONNECTING` | `CONNECTED` | `DISCONNECTED` |
| --- | --- | --- | --- |
| `accept()` | Accepts | Does nothing | `RuntimeError` |
| `send_text()`, `send_bytes()`, `send_json()` | `RuntimeError` | Sends | `RuntimeError` |
| `receive_text()`, `receive_bytes()`, `receive_json()` | `RuntimeError` | Receives | `WebSocketDisconnect` |
| `receive()` | Gives `websocket.connect` | Receives | `WebSocketDisconnect` |
| `close()` | Refuses the handshake with 403 | Closes | Does nothing |

Each `RuntimeError` message names the fix, for example `WebSocket is not accepted: call accept() before send()`.

## Require authentication

Give the route `auth` and `guards`, as for an HTTP route:

```python
from django_bolt.auth import IsAuthenticated, JWTAuthentication


@api.websocket(
    "/ws/notifications",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
)
async def notifications(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "welcome"})
```

Bolt checks the credential at the handshake, before the handler runs:

| Result | Response |
| --- | --- |
| No valid credential | `401 Unauthorized` with `{"detail":"Authentication required"}` |
| A guard fails | `403 Forbidden` |
| The token is revoked | `401 Unauthorized` |

A route with no `auth` uses `BOLT_AUTHENTICATION_CLASSES`, as an HTTP route does.
Bolt checks the token one time. An open connection stays open after the token expires or is revoked.

`SessionAuthentication` does not work on a WebSocket route, because Django middleware does not run there.
`auth=[SessionAuthentication()]` raises `ImproperlyConfigured`.
Bolt leaves a session backend of `BOLT_AUTHENTICATION_CLASSES` out of a WebSocket route.

The handler gets no `request.user`.
The guards decide if the connection opens. They do not give the user to the handler.

### Authenticate a browser

The browser `WebSocket` API cannot set the `Authorization` header.
It sends cookies, so read the token from a cookie:

```python
@api.websocket(
    "/ws/inbox",
    auth=[JWTAuthentication(cookie="access_token")],
    guards=[IsAuthenticated()],
)
async def inbox(websocket: WebSocket) -> None:
    await websocket.accept()
```

`auth` does not read a token from the query string.
See [Authentication](authentication.md).

## Allow cross-origin connections

A browser sends an `Origin` header with the handshake.
Bolt checks it against the CORS settings:

- A handshake with no `Origin` header is allowed.
- With `CORS_ALLOWED_ORIGINS`, `CORS_ALLOWED_ORIGIN_REGEXES` or `CORS_ALLOW_ALL_ORIGINS`, Bolt allows the origins that the settings allow.
- With no CORS settings, Bolt refuses each cross-origin handshake with `403` and `{"detail":"Origin not allowed"}`.

```python title="settings.py"
CORS_ALLOWED_ORIGINS = ["https://blog.example.com"]
```

See [CORS](cors.md).

## Select a subprotocol

A client can ask for a subprotocol, for example `graphql-transport-ws`.
`websocket.subprotocols` lists the subprotocols that the client asked for.
Give the one that you support to `accept()`:

```python
@api.websocket("/ws/graphql")
async def graphql(websocket: WebSocket) -> None:
    if "graphql-transport-ws" not in websocket.subprotocols:
        await websocket.close()
        return
    await websocket.accept(subprotocol="graphql-transport-ws")
```

Bolt puts it in the `Sec-WebSocket-Protocol` header of the `101` response.
A subprotocol that the client did not ask for raises `ValueError`.

`accept(headers=[(b"x-room", b"7")])` adds headers to the `101` response.
A handshake header, such as `upgrade` or `sec-websocket-protocol`, raises `ValueError`.

## Send a message to many clients

Keep the open connections in a set, and send to each of them:

```python
from collections import defaultdict

rooms: dict[int, set[WebSocket]] = defaultdict(set)


@api.websocket("/ws/articles/{article_id}/live")
async def live_room(websocket: WebSocket, article_id: int) -> None:
    await websocket.accept()
    rooms[article_id].add(websocket)
    try:
        async for text in websocket.iter_text():
            for reader in list(rooms[article_id]):
                try:
                    await reader.send_text(text)
                except (WebSocketDisconnect, RuntimeError):
                    rooms[article_id].discard(reader)  # this reader left
    finally:
        rooms[article_id].discard(websocket)
        if not rooms[article_id]:
            del rooms[article_id]  # the last reader left
```

!!! warning
    The `rooms` dict is in the memory of one process.
    With `runbolt --processes 2` or more, each process has its own `rooms`.
    Thus a message reaches only the clients of the same process.
    Use a message broker, for example Redis pub/sub, to reach the clients of all processes.

## Set the OpenAPI entry

A WebSocket route is in the OpenAPI schema with the `WebSocket` tag.
`@api.websocket()` takes `tags`, `summary`, `description`, `include_in_schema` and `name`, as an HTTP route does:

```python
@api.websocket("/ws/internal/metrics", include_in_schema=False)
async def internal_metrics(websocket: WebSocket) -> None:
    await websocket.accept()
```

See [OpenAPI](openapi.md).

## Test a WebSocket route

`WebSocketTestClient` connects to a route in the same process.
It runs the route matching, the authentication and the guards of the server.
It is async, so use it in an async test:

```python title="blog/tests.py"
import pytest

from django_bolt.testing import WebSocketTestClient

from blog.api import api


@pytest.mark.asyncio
async def test_comment_room_echoes_the_comment():
    async with WebSocketTestClient(api, "/ws/articles/7/comments") as ws:
        await ws.send_json({"body": "Nice article"})
        assert await ws.receive_json() == {"article_id": 7, "body": "Nice article"}
```

`pytest.mark.asyncio` comes from the `pytest-asyncio` package.

`WebSocketTestClient(api, path, headers=None, query_string="", subprotocols=None)` takes these arguments:

- `headers`: a `dict` of request headers. Send the credential here, for example `{"Authorization": f"Bearer {token}"}`.
- `query_string`: the query string without `?`, for example `"nickname=ada"`.
- `subprotocols`: the subprotocols that the client asks for.

The client has `send_text()`, `send_bytes()`, `send_json()`, `receive_text()`, `receive_bytes()`, `receive_json()`, `iter_text()`, `iter_bytes()`, `iter_json()` and `close()`.
Each receive method waits 5 seconds by default. Give `timeout=` to change it.

| Event | `WebSocketTestClient` raises |
| --- | --- |
| The handler closes the connection | `ConnectionClosed` on the next receive, with `code` and `reason` |
| The handler refuses the handshake | `HandshakeRejected` at `async with`, with `status_code` |
| The authentication, a guard or the Origin check refuses the handshake | `PermissionError` at `async with` |
| A request value does not convert or fails its check, or the handler raises `RequestValidationError` before `accept()` (the server answers `400`) | `ValueError` at `async with`, with the detail of the server |
| The handler raises before `accept()` | That exception at `async with` |
| The handler raises after `accept()` | That exception when the `async with` block ends |

```python
from django_bolt.testing import ConnectionClosed, HandshakeRejected


@pytest.mark.asyncio
async def test_read_only_article_closes_with_4001():
    async with WebSocketTestClient(api, "/ws/articles/7/edit") as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await ws.receive_text()
    assert closed.value.code == 4001
    assert closed.value.reason == "Read-only article"


@pytest.mark.asyncio
async def test_unknown_draft_is_refused():
    with pytest.raises(HandshakeRejected) as rejected:
        async with WebSocketTestClient(api, "/ws/drafts/500/preview"):
            pass
    assert rejected.value.status_code == 403
```

At the end of the `async with` block, the client disconnects with code 1000.
The handler gets the disconnect and can run to its end.
The client cancels a handler that still runs after 1 second.

`TestClient` has no WebSocket method. See [Testing](testing.md).

## Deploy WebSocket routes

`runbolt` serves WebSocket routes on the same port as HTTP routes.
A reverse proxy in front of Bolt must pass the `Upgrade` and `Connection` headers.
In nginx, set `proxy_http_version 1.1`, `proxy_set_header Upgrade $http_upgrade` and `proxy_set_header Connection "upgrade"`.

Bolt reads these settings one time, at startup.
An environment variable with the `DJANGO_BOLT_` prefix overrides the setting, for example `DJANGO_BOLT_WS_MAX_CONNECTIONS`.

| Setting | Default | Effect |
| --- | --- | --- |
| `BOLT_WS_MAX_CONNECTIONS` | `10000` | Open connections in one process. A new handshake over the limit gets `503`. |
| `BOLT_WS_MAX_MESSAGE_SIZE` | `1048576` (1 MiB) | A larger message closes the connection with code 1009. |
| `BOLT_WS_HEARTBEAT_INTERVAL` | `5` | Seconds between the pings that Bolt sends. |
| `BOLT_WS_CLIENT_TIMEOUT` | `10` | Seconds with no frame and no pong from the client. Bolt then closes the connection, and the handler gets code 1001. |
| `BOLT_WS_CHANNEL_SIZE` | `100` | Messages from the client that wait for the handler. When the handler falls behind and the queue is full, Bolt closes the connection with code 1011. |

When `runbolt` drains a worker, for example on `SIGTERM` or a recycle, it closes each open connection with code 1012 (Service Restart).
A client can connect again and reach a different worker.
A stop at once, for example on `SIGINT`, closes the connections with no close code. Thus a client must also handle an abnormal close.

## See also

- [Server-Sent Events and streaming](sse.md)
- [Authentication](authentication.md)
- [Permissions](permissions.md)
- [ASGI mounts](asgi-mounts.md) for Django Channels and other ASGI WebSocket apps
- [BoltAPI reference](../ref/api.md)
- [Testing reference](../ref/testing.md)

---
icon: lucide/cable
---

# WebSocket

Django-Bolt provides WebSocket support for real-time bidirectional communication.

## Basic WebSocket endpoint

```python
from django_bolt import BoltAPI, WebSocket

api = BoltAPI()

@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"Echo: {message}")
```

## WebSocket lifecycle

A WebSocket connection goes through these stages:

1. **Connection** - Client initiates WebSocket handshake
2. **Accept** - The handler calls `accept()`. Bolt then sends the `101 Switching Protocols` response.
3. **Communication** - Exchange messages
4. **Close** - Either party closes the connection

```python
@api.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    # 1. Accept the connection
    await websocket.accept()

    try:
        # 2. Communication loop
        while True:
            message = await websocket.receive_text()
            await websocket.send_text(f"You said: {message}")
    except WebSocketDisconnect:
        # 3. Client disconnected
        pass
```

## Sending messages

### Text messages

```python
await websocket.send_text("Hello, World!")
```

### Binary messages

```python
await websocket.send_bytes(b"\x00\x01\x02\x03")
```

### JSON messages

```python
await websocket.send_json({"type": "message", "data": "Hello"})
```

## Receiving messages

### Text messages

```python
# Single message
message = await websocket.receive_text()

# Iterate over messages
async for message in websocket.iter_text():
    print(f"Received: {message}")
```

### Binary messages

```python
data = await websocket.receive_bytes()

async for data in websocket.iter_bytes():
    print(f"Received {len(data)} bytes")
```

### JSON messages

```python
data = await websocket.receive_json()

async for data in websocket.iter_json():
    print(f"Received: {data}")
```

## Path parameters

WebSocket routes support path parameters:

```python
@api.websocket("/ws/room/{room_id}")
async def room(websocket: WebSocket, room_id: str):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"[{room_id}] {message}")
```

Bolt decodes path values as for HTTP routes. Thus `/ws/room/hello%20world`
gives `hello world`.

## Query parameters

Access query parameters from the connection:

```python
@api.websocket("/ws/connect")
async def connect(websocket: WebSocket, token: str | None = None):
    if token != "secret":
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    # ...
```

When a query key repeats, the parameter gets the last value, as for HTTP
routes.

## Typed parameters

Declare a path, query, header or cookie parameter with a type, for example
`int`. Rust converts the value before the handler runs. A value that does not
convert rejects the upgrade with a 400. The body names the parameter:

```python
from typing import Annotated

from django_bolt.param_functions import Query

@api.websocket("/ws/feed")
async def feed(websocket: WebSocket, limit: Annotated[int, Query()] = 10):
    await websocket.accept()
    await websocket.send_json({"limit": limit})
```

`?limit=50` gives the integer `50`. `?limit=abc` gives a 400 with
`Query parameter 'limit': Invalid integer 'abc'`. `WebSocketTestClient`
raises `ValueError` with the same text.

## Closing connections

### From the server

```python
await websocket.close()

# With custom close code and reason
await websocket.close(code=1000, reason="Normal closure")
```

### Refuse the handshake

Bolt sends the `101` response only when the handler calls `accept()`.
Before that, the handler can refuse the connection with an HTTP status:

| Handler action before `accept()` | Response |
|----------------------------------|----------|
| Calls `close()` | `403 Forbidden` |
| Returns | `403 Forbidden` |
| Raises an exception | `500 Internal Server Error` |

```python
@api.websocket("/ws/rooms/{room}")
async def room(websocket: WebSocket, room: str):
    if room not in OPEN_ROOMS:
        await websocket.close()  # The client gets 403. The connection does not open.
        return
    await websocket.accept()
```

A call to `receive()` before `accept()` gives `{"type": "websocket.connect"}`
one time, as the ASGI specification requires. A second call waits until the
handler accepts or refuses the handshake.

### Handling client disconnect

```python
from django_bolt import WebSocketDisconnect

@api.websocket("/ws")
async def handler(websocket: WebSocket):
    await websocket.accept()
    try:
        async for message in websocket.iter_text():
            await websocket.send_text(message)
    except WebSocketDisconnect:
        print("Client disconnected")
```

A send to a client that left raises `WebSocketDisconnect` with code 1006.
Bolt does not log it as a handler error.

## Close codes

Common WebSocket close codes:

| Code | Name | Description |
|------|------|-------------|
| 1000 | Normal Closure | Normal closure |
| 1001 | Going Away | Server/client going away |
| 1002 | Protocol Error | Protocol error |
| 1003 | Unsupported Data | Unsupported data type |
| 1008 | Policy Violation | Policy violation |
| 1011 | Server Error | Server encountered error |

Access close codes:

```python
from django_bolt import CloseCode

await websocket.close(code=CloseCode.NORMAL_CLOSURE)
```

## Subprotocols

A client can request one or more subprotocols in the `Sec-WebSocket-Protocol`
header. `websocket.subprotocols` gives the list, in the order of the client.
Select one with `accept(subprotocol=...)`. Bolt sends it in the `101` response:

```python
@api.websocket("/graphql")
async def graphql(websocket: WebSocket):
    if "graphql-transport-ws" not in websocket.subprotocols:
        await websocket.close()
        return
    await websocket.accept(subprotocol="graphql-transport-ws")
```

```javascript
const ws = new WebSocket("ws://localhost:8000/graphql", ["graphql-transport-ws"]);
ws.onopen = () => console.log(ws.protocol); // "graphql-transport-ws"
```

A browser closes the connection if it requests a subprotocol and the `101`
response does not select one. RFC 6455 lets the server select only a
subprotocol that the client requested. Thus `accept()` raises `ValueError`
for any other value, and the handshake gets `500`.

The scope also has the list, in `scope["subprotocols"]`, as the ASGI
specification requires.

### Response headers

Give `accept()` extra headers for the `101` response as `(name, value)` byte pairs:

```python
await websocket.accept(headers=[(b"x-session-id", session_id.encode())])
```

The handshake sets `Sec-WebSocket-Protocol`, `Sec-WebSocket-Accept`,
`Sec-WebSocket-Extensions`, `Upgrade` and `Connection`. `accept()` raises
`ValueError` for these headers. Select the subprotocol with `subprotocol=...`.

## Origin policy

WebSocket opening handshakes are checked independently of HTTP CORS response
headers. Browsers send `Origin` even for same-origin WebSockets. Origin is
not authentication; non-browser clients can omit or forge it.

With no WebSocket-specific configuration, Bolt retains its historical
policy: it rejects a present Origin when CORS is absent. Otherwise, it checks Django's
`CORS_ALLOWED_ORIGINS` / `CORS_ALLOWED_ORIGIN_REGEXES` /
`CORS_ALLOW_ALL_ORIGINS`, and permits an absent Origin.

Previously, native routes and ASGI mounts shared this global CORS check.
They could not select separate allowlists or custom validators.
A mount could not explicitly transfer validation to its own middleware.
These policies add those controls without changing the default behavior.

Exact allowlist matching does not normalize case, ports, or host names.
Use the exact Origin that the browser sends.
For example, `https://app.example` and `https://app.example:443` differ.
Use ASCII host names, including punycode for international domain names.
The `allowlist` and `any` policies accept IPv4 and bracketed IPv6 addresses.
Ports must be between 1 and 65535. An empty port is invalid.
Allowlist entries cannot contain credentials, paths, queries, fragments, or wildcards.
These syntax restrictions apply to allowlist entries and the `any` policy.
Custom validators decide which Origin values they permit.

### Global defaults: settings.py

```python
BOLT_WEBSOCKET_ORIGIN_POLICY = "allowlist"  # inherit / allowlist / any / custom
BOLT_WEBSOCKET_ALLOWED_ORIGINS = ["https://app.example.com"]
BOLT_WEBSOCKET_ALLOW_MISSING_ORIGIN = False
# Only for policy="custom":
# BOLT_WEBSOCKET_ORIGIN_VALIDATOR = "myproject.security.validate_tenant_origin"
```

An endpoint overrides only the explicitly provided fields. These defaults
apply to both native WebSocket routes and ASGI WebSocket mounts. Defaults
without any new settings remain backward-compatible.

### Per-native-route options

```python
@api.websocket(
    "/ws/partners",
    origin_policy="allowlist",
    allowed_origins=["https://partner.example.com"],
    allow_missing_origin=False,
)
async def partner_socket(ws: WebSocket):
    await ws.accept()
```

An `"any"` policy permits arbitrary syntactically valid HTTP(S) web Origins,
but **not** `Origin: null`, malformed Origins, or a missing Origin unless
`allow_missing_origin=True` was explicitly configured. An `"allowlist"`
policy uses exact origins and is independent of global CORS. An `"inherit"`
policy preserves the legacy missing-Origin behavior unless overridden.

### Custom validation

```python
from django_bolt.websocket.origin_policy import OriginContext


def validate_tenant_origin(origin: str | None, context: OriginContext) -> bool:
    # Validate origin against trusted tenant configuration. Client-supplied
    # Host/headers and path strings are NOT authentication or tenant proof.
    return origin == "https://tenant.example.com"

@api.websocket(
    "/ws/tenant",
    origin_policy="custom",
    origin_validator=validate_tenant_origin,
)
async def tenant_socket(ws: WebSocket):
    await ws.accept()
```

A validator may also be `async def`:

```python
async def validate_origin_async(origin: str | None, context: OriginContext) -> bool:
    return origin == "https://tenant.example.com"
```

A validator receives `(origin, context)`;
`context` contains `path`, `query_string` and lower-case `headers`.
It **must return a bool**. Exceptions, non-boolean values and timeouts
(5 seconds) reject the handshake with HTTP 403. Missing Origin is rejected
by default for custom policies unless allowed explicitly; even then the
validator still has to approve it. Python runs only for `custom`, once per
handshake, and is slower than the Rust-only policies. Avoid slow database
operations or untrusted tenant identifiers. Never use Origin as auth.
Sync callbacks run in a separate, lazily created pool (up to eight running
callbacks per process), not the general handler executor. Up to 64 custom
validations may be pending per process; excess handshakes fail closed. When a
sync callback times out, its thread may continue running and keeps its
execution slot until it finishes. Dedicated daemon workers do not block process
exit, but the process may stop them without cleanup. The Python deadline cancels
cooperative async callbacks before Rust's five-second safety timeout; callbacks
that suppress cancellation can hold admission until they finish. Keep callbacks
bounded and free of side effects.
The context path includes the mount prefix. The query string is a string without `?`.
Headers are client input, not trusted tenant or identity data.

### Mounted ASGI applications

`api.mount_asgi` and custom `api.mount_django` accept the same options,
with the `websocket_` prefix, e.g. `websocket_origin_policy="custom"`,
`websocket_origin_validator=...`, `websocket_allow_missing_origin=False`.
Mounts can also explicitly use `"delegate"` when the ASGI app already
checks Origin itself, for example via Django Channels' `OriginValidator`.
This is a security-sensitive opt-out, not an Origin-validation mode for
native Bolt routes. See [ASGI mounts](asgi-mounts.md#middleware-boundary).

### Validation and migration

Invalid policies and allowlists raise `ValueError` at registration or startup.
A missing custom callable or incompatible local option also raises `ValueError`.
A non-boolean `allow_missing_origin` raises `TypeError`.
A dotted validator path must import successfully.
Only the selected policy uses its global allowlist or validator.
Local allowlists and validators require their corresponding policy.

Existing applications need no changes when the new settings are absent.
Each explicit field overrides its global counterpart; other fields still use settings.
Thus, a global missing-Origin flag also applies when a route selects another policy.
Explicit `"inherit"` selects CORS behavior, even under a global custom policy.
`"delegate"` is valid only as an explicit mount option, never as a global setting.
It bypasses the missing-Origin flag too.

Bolt rejects repeated Origin header lines, except when a mount explicitly delegates validation.
Previously, different checks could read different lines. Do not send repeated Origin headers.
The `inherit` policy otherwise retains its existing matching rules.

All checks finish before HTTP 101. Native authentication and guards still apply.
Mounted applications enforce their own authentication and guards, regardless of Origin policy.
Enabling `"any"` or `"delegate"` changes the security boundary. Review cookie authentication before enabling either option.

## Authentication

Apply authentication to WebSocket endpoints:

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated

@api.websocket(
    "/ws/protected",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()]
)
async def protected(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("Welcome")
```

The handshake reads the token from the request headers. Send the token in
the `Authorization` header:

```python
import websockets

async with websockets.connect(
    "ws://localhost:8000/ws/protected",
    additional_headers={"Authorization": f"Bearer {token}"},
) as ws:
    print(await ws.recv())
```

### Revoked tokens

A `revoked_token_handler` or a `revocation_store` of the backend applies to
the handshake too. When the handler reports the token as revoked, the
handshake gets `401 Unauthorized` and the connection does not open. Bolt
checks the token one time, at the handshake. An open connection stays open
after its token is revoked. To end it, close it from the handler. See
[Token revocation](authentication.md#token-revocation).

### Authentication from a browser

The browser `WebSocket` API cannot set request headers. Use a cookie instead.
The browser sends cookies with the handshake:

```python
@api.websocket(
    "/ws/protected",
    auth=[JWTAuthentication(cookie="access_token")],
    guards=[IsAuthenticated()],
)
async def protected(websocket: WebSocket):
    await websocket.accept()
```

```javascript
// The browser sends the access_token cookie with the handshake.
const ws = new WebSocket("ws://localhost:8000/ws/protected");
```

A query parameter does not authenticate the handshake. `auth=[...]` reads only
the headers. To use a token from the URL, validate the token in the handler.
Close the connection if the token is not valid. See
[Query parameters](#query-parameters).

## WebSocket state

Check the connection state:

```python
from django_bolt import WebSocketState

if websocket.state == WebSocketState.CONNECTED:
    await websocket.send_text("Still connected")
```

States:

- `WebSocketState.CONNECTING` - Before `accept()`
- `WebSocketState.CONNECTED` - After `accept()`
- `WebSocketState.DISCONNECTED` - After close

## OpenAPI metadata

A WebSocket route appears in the OpenAPI schema with the `WebSocket` tag.
Use `tags` to set different tags.
Use `summary` and `description` to replace the text from the docstring.
Set `include_in_schema=False` to keep the route out of the schema:

```python
@api.websocket(
    "/ws/stream",
    tags=["Streaming"],
    summary="Stream prices",
    description="Sends each price change.",
)
async def stream(websocket: WebSocket):
    await websocket.accept()

@api.websocket("/ws/internal", include_in_schema=False)
async def internal(websocket: WebSocket):
    await websocket.accept()
```

A route without `include_in_schema` uses the value of its `BoltAPI`, as an HTTP route does.

## Testing WebSockets

Use the `WebSocketTestClient`:

```python
from django_bolt.testing import TestClient

with TestClient(api) as client:
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("Hello")
        response = ws.receive_text()
        assert response == "Echo: Hello"
```

`WebSocketTestClient` waits for the handshake decision of the handler, as the
server does. A refused handshake fails the `async with`:

```python
from django_bolt.testing import HandshakeRejected, WebSocketTestClient

with pytest.raises(HandshakeRejected) as rejected:
    async with WebSocketTestClient(api, "/ws/rooms/closed"):
        pass
assert rejected.value.status_code == 403
```

An error in the handler before `accept()` fails the `async with` with that error.

## Real-time patterns

### Broadcast to all clients

```python
connected_clients = set()

@api.websocket("/ws/broadcast")
async def broadcast(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)

    try:
        async for message in websocket.iter_text():
            # Broadcast to all clients
            for client in connected_clients:
                await client.send_text(message)
    finally:
        connected_clients.discard(websocket)
```

### Room-based chat

```python
rooms = {}  # room_id -> set of websockets

@api.websocket("/ws/room/{room_id}")
async def room(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in rooms:
        rooms[room_id] = set()
    rooms[room_id].add(websocket)

    try:
        async for message in websocket.iter_text():
            for client in rooms[room_id]:
                await client.send_text(f"[{room_id}] {message}")
    finally:
        rooms[room_id].discard(websocket)
```

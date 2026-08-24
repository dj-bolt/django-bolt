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
2. **Accept** - Server accepts the connection
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

## Closing connections

### From the server

```python
await websocket.close()

# With custom close code and reason
await websocket.close(code=1000, reason="Normal closure")
```

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

The handshake reads the token from the request headers. Send it in the
`Authorization` header:

```python
import websockets

async with websockets.connect(
    "ws://localhost:8000/ws/protected",
    additional_headers={"Authorization": f"Bearer {token}"},
) as ws:
    print(await ws.recv())
```

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

A query parameter does not authenticate the handshake. `auth=[...]` reads the
headers only. To use a token from the URL, validate it in the handler and close
the connection yourself — see [Query parameters](#query-parameters).

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

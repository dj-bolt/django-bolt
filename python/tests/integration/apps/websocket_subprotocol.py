"""App under test for WebSocket subprotocol negotiation over real TCP.

Each route makes one handshake decision: accept with a subprotocol, accept
with extra headers, close before accept, or fail before accept.
"""

from __future__ import annotations

import asyncio

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    requested = websocket.subprotocols
    await websocket.accept(subprotocol="chat.v2" if "chat.v2" in requested else None)
    await websocket.send_text(",".join(requested))
    await websocket.receive_text()


@api.websocket("/ws/headers")
async def with_headers(websocket: WebSocket):
    await websocket.accept(headers=[(b"x-session", b"abc123")])
    await websocket.send_text("ok")
    await websocket.receive_text()


@api.websocket("/ws/unrequested")
async def unrequested(websocket: WebSocket):
    await websocket.accept(subprotocol="not-requested")


@api.websocket("/ws/refuse")
async def refuse(websocket: WebSocket):
    await websocket.close()


@api.websocket("/ws/fail")
async def fail(websocket: WebSocket):
    raise RuntimeError("boom before accept")


@api.websocket("/ws/no-accept")
async def no_accept(websocket: WebSocket):
    return


@api.websocket("/ws/connect-first")
async def connect_first(websocket: WebSocket):
    message = await websocket.receive()
    await websocket.accept()
    await websocket.send_text(message["type"])
    await websocket.receive_text()


# Each entry names what a send raised after the client left during the handshake.
SEND_AFTER_LEAVE: list[str] = []


@api.websocket("/ws/slow-accept")
async def slow_accept(websocket: WebSocket):
    await asyncio.sleep(0.5)
    await websocket.accept()
    try:
        await websocket.send_text("late")
    except Exception as exc:
        SEND_AFTER_LEAVE.append(type(exc).__name__)
    else:
        SEND_AFTER_LEAVE.append("no error")


@api.get("/send-after-leave")
async def send_after_leave():
    return {"errors": SEND_AFTER_LEAVE}


async def negotiating_app(scope, receive, send):
    """Mounted ASGI app: refuses on /refuse, else accepts the first subprotocol."""
    connect = await receive()
    if connect["type"] != "websocket.connect":
        raise AssertionError(f"expected websocket.connect, got {connect['type']}")
    if scope["path"].endswith("/refuse"):
        await send({"type": "websocket.close", "code": 1000})
        return
    subprotocols = scope["subprotocols"]
    await send(
        {
            "type": "websocket.accept",
            "subprotocol": subprotocols[0] if subprotocols else None,
            "headers": [(b"x-mounted", b"yes")],
        }
    )
    await send({"type": "websocket.send", "text": ",".join(subprotocols)})
    while (await receive())["type"] != "websocket.disconnect":
        pass


api.mount_asgi("/mounted", negotiating_app)

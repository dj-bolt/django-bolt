"""App under test for WebSocket subprotocol negotiation over real TCP.

Each route makes one handshake decision: accept with a subprotocol, accept
with extra headers, close before accept, or fail before accept.
"""

from __future__ import annotations

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

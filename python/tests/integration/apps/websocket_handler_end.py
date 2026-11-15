"""App under test for the end of a WebSocket handler over real TCP.

Each route accepts the connection. Then it returns with no ``close()``, it
raises, or it closes. The mounted ASGI app returns with no ``websocket.close``.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/return")
async def return_after_accept(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("bye")


@api.websocket("/ws/raise")
async def raise_after_accept(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("bye")
    raise RuntimeError("handler failed")


@api.websocket("/ws/close-then-return")
async def close_then_return(websocket: WebSocket):
    await websocket.accept()
    await websocket.close(code=4001, reason="own close")


async def returning_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.send", "text": "bye"})


api.mount_asgi("/raw", returning_app)

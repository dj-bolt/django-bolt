"""App under test for WebSocket parameter-length enforcement over real TCP.

A path-param route and a plain route, each accepting the connection and sending
one frame. The point of interest is the *upgrade*: oversized path/query params
must be rejected with an HTTP 400 before the handler runs.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/path/{value}")
async def path_ws(websocket: WebSocket, value: str):
    await websocket.accept()
    await websocket.send_text("connected")


@api.websocket("/ws/plain")
async def plain_ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("connected")

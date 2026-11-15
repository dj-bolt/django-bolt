"""App under test for WebSocket route paths that a client sends with escapes.

Each route sends its route path, and the path parameter if it has one. A client
sends a space or a non-ASCII character of a path as a `%XX` escape.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/ü")
async def non_ascii_ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("/ws/ü")


@api.websocket("/ws/a b")
async def space_ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("/ws/a b")


@api.websocket("/ws/ü b/{room}")
async def room_ws(websocket: WebSocket, room: str):
    await websocket.accept()
    await websocket.send_text(f"/ws/ü b/{room}")

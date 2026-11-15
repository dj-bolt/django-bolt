"""App under test for a frame of the wrong type over real TCP.

``/ws/text`` reads with ``receive_text()`` and ``/ws/bytes`` reads with
``receive_bytes()``. Each route sends back what it read, or the error.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket, WebSocketException

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/text")
async def read_text(websocket: WebSocket):
    await websocket.accept()
    while True:
        try:
            await websocket.send_text(f"text: {await websocket.receive_text()}")
        except WebSocketException as error:
            if error.code != 1003:
                raise
            await websocket.send_text(f"{error.code}: {error.reason}")


@api.websocket("/ws/bytes")
async def read_bytes(websocket: WebSocket):
    await websocket.accept()
    while True:
        try:
            await websocket.send_text(f"bytes: {(await websocket.receive_bytes()).hex()}")
        except WebSocketException as error:
            if error.code != 1003:
                raise
            await websocket.send_text(f"{error.code}: {error.reason}")

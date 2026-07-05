"""App under test for graceful WebSocket drain on shutdown.

A plain echo socket: the test opens it, triggers SIGTERM, and asserts the server
closes the connection with 1012 (Service Restart) instead of dropping it.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"echo:{message}")

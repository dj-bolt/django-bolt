"""App under test for the WebSocket handshake + auth over real TCP.

Accepts the connection, rejects clients without the expected bearer token, then
echoes text frames until the client sends ``bye``.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket
from django_bolt.websocket import CloseCode

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/protected")
async def protected(websocket: WebSocket):
    await websocket.accept()

    if websocket.headers.get("authorization") != "Bearer secret-token":
        await websocket.close(code=CloseCode.POLICY_VIOLATION, reason="unauthorized")
        return

    await websocket.send_text("authorized")
    async for message in websocket.iter_text():
        if message == "bye":
            await websocket.close(code=CloseCode.NORMAL, reason="done")
            return
        await websocket.send_text(f"echo:{message}")

"""App under test for the Origin check of a WebSocket handshake over real TCP.

The test sets the CORS settings in ``settings_extra``. The route accepts each
connection that passes the Origin check.
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
    await websocket.send_text("accepted")

"""App under test for the close frame of a WebSocket over real TCP.

Each route calls ``close()`` with a code and a reason. A close that the
server refuses raises ``ValueError``. The route sends the error text, and then
it closes with 1000. The mounted ASGI app sends the raw ``websocket.close``.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


async def _close_and_report(websocket: WebSocket, code: int, reason: str) -> None:
    await websocket.accept()
    try:
        await websocket.close(code=code, reason=reason)
    except ValueError as error:
        await websocket.send_text(f"ValueError: {error}")
        await websocket.close(reason="refused")


@api.websocket("/ws/reason-200-bytes")
async def reason_200_bytes(websocket: WebSocket):
    await _close_and_report(websocket, 1000, "x" * 200)


@api.websocket("/ws/reason-124-bytes-62-characters")
async def reason_124_bytes(websocket: WebSocket):
    await _close_and_report(websocket, 1000, "é" * 62)


@api.websocket("/ws/reason-123-bytes")
async def reason_123_bytes(websocket: WebSocket):
    await _close_and_report(websocket, 4001, "é" * 61 + "x")


@api.websocket("/ws/code-70000")
async def code_70000(websocket: WebSocket):
    await _close_and_report(websocket, 70000, "")


@api.websocket("/ws/code-1006")
async def code_1006(websocket: WebSocket):
    await _close_and_report(websocket, 1006, "")


async def raw_close_app(scope, receive, send):
    """Send the close message of the path with no ``WebSocket`` wrapper."""
    await receive()
    await send({"type": "websocket.accept"})
    closes = {
        "/raw/reason-200-bytes": {"code": 1000, "reason": "x" * 200},
        "/raw/code-70000": {"code": 70000},
    }
    try:
        await send({"type": "websocket.close", **closes[scope["path"]]})
    except ValueError as error:
        await send({"type": "websocket.send", "text": f"ValueError: {error}"})
        await send({"type": "websocket.close", "code": 1000, "reason": "refused"})


api.mount_asgi("/raw", raw_close_app)

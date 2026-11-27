"""
Django-Bolt WebSocket support with FastAPI-like syntax.

Example:
    from django_bolt import BoltAPI
    from django_bolt.websocket import WebSocket

    api = BoltAPI()

    @api.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Echo: {data}")

    @api.websocket("/ws/{room_id}")
    async def room_websocket(websocket: WebSocket, room_id: str):
        await websocket.accept()
        await websocket.send_json({"room": room_id, "status": "connected"})
        async for message in websocket.iter_json():
            await websocket.send_json({"echo": message})
"""

from __future__ import annotations

from .close_codes import (
    WS_1000_NORMAL_CLOSURE,
    WS_1001_GOING_AWAY,
    WS_1002_PROTOCOL_ERROR,
    WS_1003_UNSUPPORTED_DATA,
    WS_1008_POLICY_VIOLATION,
    WS_1009_MESSAGE_TOO_BIG,
    WS_1011_INTERNAL_ERROR,
    WS_1012_SERVICE_RESTART,
    WS_1013_TRY_AGAIN_LATER,
    CloseCode,
)
from .exceptions import (
    WebSocketClose,
    WebSocketDisconnect,
    WebSocketException,
)
from .handlers import (
    get_websocket_param_name,
    mark_websocket_handler,
)
from .state import WebSocketState
from .types import WebSocket

__all__ = [
    # Main type
    "WebSocket",
    # State
    "WebSocketState",
    # Close codes
    "CloseCode",
    "WS_1000_NORMAL_CLOSURE",
    "WS_1001_GOING_AWAY",
    "WS_1002_PROTOCOL_ERROR",
    "WS_1003_UNSUPPORTED_DATA",
    "WS_1008_POLICY_VIOLATION",
    "WS_1009_MESSAGE_TOO_BIG",
    "WS_1011_INTERNAL_ERROR",
    "WS_1012_SERVICE_RESTART",
    "WS_1013_TRY_AGAIN_LATER",
    # Exceptions
    "WebSocketException",
    "WebSocketDisconnect",
    "WebSocketClose",
    # Handler utilities
    "mark_websocket_handler",
    "get_websocket_param_name",
]

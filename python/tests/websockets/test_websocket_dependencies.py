"""A WebSocket handler binds the parameters of its dependencies, as an HTTP handler does.

The real-server counterpart is ``integration/test_websocket_dependencies_server_integration.py``.
"""

from __future__ import annotations

import json
from typing import Annotated

import pytest

from django_bolt import BoltAPI, Depends, WebSocket
from django_bolt.param_functions import Header, Query
from django_bolt.testing import WebSocketTestClient


def room_settings(
    room: Annotated[str, Query()] = "lobby",
    limit: Annotated[int, Query()] = 10,
    x_team: Annotated[str, Header()] = "none",
) -> dict:
    return {"room": room, "limit": limit, "limit_type": type(limit).__name__, "team": x_team}


def _api() -> BoltAPI:
    api = BoltAPI()

    @api.websocket("/ws")
    async def ws(websocket: WebSocket, settings=Depends(room_settings)):
        await websocket.accept()
        await websocket.send_text(json.dumps(settings))
        await websocket.close()

    return api


@pytest.mark.asyncio
async def test_a_websocket_dependency_gets_its_query_and_header_values():
    async with WebSocketTestClient(
        _api(), "/ws", query_string="room=blue&limit=3", headers={"X-Team": "red"}
    ) as websocket:
        message = json.loads(await websocket.receive_text())

    assert message == {"room": "blue", "limit": 3, "limit_type": "int", "team": "red"}


@pytest.mark.asyncio
async def test_a_websocket_dependency_gets_its_defaults():
    async with WebSocketTestClient(_api(), "/ws") as websocket:
        message = json.loads(await websocket.receive_text())

    assert message == {"room": "lobby", "limit": 10, "limit_type": "int", "team": "none"}

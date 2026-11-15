"""A WebSocket handler binds the parameters of its dependencies, as an HTTP handler does.

The real-server counterpart is ``integration/test_websocket_dependencies_server_integration.py``.
"""

from __future__ import annotations

import asyncio
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


async def room_settings_async(
    room: Annotated[str, Query()] = "lobby",
    limit: Annotated[int, Query()] = 10,
    x_team: Annotated[str, Header()] = "none",
) -> dict:
    await asyncio.sleep(0)
    return room_settings(room, limit, x_team)


def _api() -> BoltAPI:
    api = BoltAPI()

    @api.websocket("/ws")
    async def ws(websocket: WebSocket, settings=Depends(room_settings)):
        await websocket.accept()
        await websocket.send_text(json.dumps(settings))
        await websocket.close()

    @api.websocket("/ws-async")
    async def ws_async(websocket: WebSocket, settings=Depends(room_settings_async)):
        await websocket.accept()
        await websocket.send_text(json.dumps(settings))
        await websocket.close()

    return api


PATHS = pytest.mark.parametrize("path", ["/ws", "/ws-async"], ids=["sync-dependency", "async-dependency"])


@pytest.mark.asyncio
@PATHS
async def test_a_websocket_dependency_gets_its_query_and_header_values(path):
    async with WebSocketTestClient(
        _api(), path, query_string="room=blue&limit=3", headers={"X-Team": "red"}
    ) as websocket:
        message = json.loads(await websocket.receive_text())

    assert message == {"room": "blue", "limit": 3, "limit_type": "int", "team": "red"}


@pytest.mark.asyncio
@PATHS
async def test_a_websocket_dependency_gets_its_defaults(path):
    async with WebSocketTestClient(_api(), path) as websocket:
        message = json.loads(await websocket.receive_text())

    assert message == {"room": "lobby", "limit": 10, "limit_type": "int", "team": "none"}

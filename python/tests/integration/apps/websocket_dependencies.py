"""App under test for the dependencies of a WebSocket handler over a real server.

Each dependency reads a query value and a header. One dependency is sync and one is async. The handler sends back what
the dependency got, with the type name of the typed value.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from django_bolt import BoltAPI, Depends, WebSocket
from django_bolt.param_functions import Header, Query

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


def room_settings(
    limit: Annotated[int, Query()] = 10,
    x_team: Annotated[str, Header()] = "none",
) -> dict:
    return {"limit": limit, "limit_type": type(limit).__name__, "team": x_team}


async def room_settings_async(
    limit: Annotated[int, Query()] = 10,
    x_team: Annotated[str, Header()] = "none",
) -> dict:
    await asyncio.sleep(0)
    return room_settings(limit, x_team)


@api.websocket("/ws")
async def room(websocket: WebSocket, settings=Depends(room_settings)):
    await websocket.accept()
    await websocket.send_text(json.dumps(settings))


@api.websocket("/ws-async")
async def room_async(websocket: WebSocket, settings=Depends(room_settings_async)):
    await websocket.accept()
    await websocket.send_text(json.dumps(settings))

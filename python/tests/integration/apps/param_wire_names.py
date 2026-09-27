"""App under test for path and query type conversion by wire name over a real server.

Each route declares a typed parameter whose alias is not its Python name.
The routes return the value and its type name, so a test can see the conversion.
"""

from __future__ import annotations

import json
from typing import Annotated

from django_bolt import BoltAPI, WebSocket
from django_bolt.param_functions import Path, Query

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/query")
async def aliased_query(page: Annotated[int, Query(alias="p")]):
    return {"page": page, "type": type(page).__name__}


@api.get("/items/{id}")
def aliased_path(item_id: Annotated[int, Path(alias="id")]):
    return {"item_id": item_id, "type": type(item_id).__name__}


@api.websocket("/ws/rooms/{id}")
async def room(
    websocket: WebSocket,
    room_id: Annotated[int, Path(alias="id")],
    page: Annotated[int, Query(alias="p")],
):
    await websocket.accept()
    await websocket.send_text(json.dumps({"room_id": type(room_id).__name__, "page": type(page).__name__}))

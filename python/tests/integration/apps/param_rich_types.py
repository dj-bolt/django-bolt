"""App under test for sequence, NewType and constrained parameters over a real server.

Each route returns the value and its type name, so a test can see the conversion.
"""

from __future__ import annotations

from typing import Annotated, NewType

import msgspec

from django_bolt import BoltAPI, WebSocket
from django_bolt.param_functions import Query

api = BoltAPI()

UserId = NewType("UserId", int)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/tags")
async def tags(tag: Annotated[set[int], Query()]):
    return {"tag": sorted(tag), "type": type(tag).__name__}


@api.get("/users/{user_id}")
def user(user_id: UserId, page: Annotated[int, msgspec.Meta(ge=1)] = 1):
    return {"user_id": user_id, "type": type(user_id).__name__, "page": page}


@api.websocket("/ws/tags")
async def ws_tags(websocket: WebSocket, tag: Annotated[list[int], Query()]):
    await websocket.accept()
    await websocket.send_json({"tag": tag})

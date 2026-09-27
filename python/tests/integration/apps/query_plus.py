"""App under test for the decode of `+` in a query over a real server.

The routes echo a query value and a path value.
A test can then see that `+` is a space in a query and a `+` in a path.
"""

from __future__ import annotations

import json

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/search")
async def search(q: str):
    return {"q": q}


@api.get("/items/{name}")
def item(name: str):
    return {"name": name}


@api.websocket("/ws/search")
async def search_ws(websocket: WebSocket, q: str):
    await websocket.accept()
    await websocket.send_text(json.dumps({"q": q}))

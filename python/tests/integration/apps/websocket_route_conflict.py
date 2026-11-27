"""App under test: two WebSocket routes at one position stop startup.

``runbolt`` must stop with a ``CommandError`` that names both routes, as it
does for two HTTP routes.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/live/{room}")
async def room(websocket):
    await websocket.accept()


@api.websocket("/live/{rest:path}")
async def rest(websocket):
    await websocket.accept()

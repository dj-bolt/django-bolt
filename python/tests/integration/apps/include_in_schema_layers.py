"""App under test for layered ``include_in_schema`` and WebSocket OpenAPI metadata.

A hidden sub-API, a hidden view class, and a hidden route. One route in the
hidden sub-API opts back in. All routes are still served.
WebSocket routes use the same layers, and set ``tags`` and ``summary``.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.views import APIView
from django_bolt.websocket import WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/public")
async def public():
    return {"public": True}


@api.get("/route-hidden", include_in_schema=False)
async def route_hidden():
    return {"hidden": "route"}


@api.view("/view-hidden")
class HiddenView(APIView):
    include_in_schema = False

    async def get(self, request):
        return {"hidden": "view"}


async def _echo_path(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text(websocket.path)
    await websocket.close()


@api.websocket("/ws/default")
async def ws_default(websocket: WebSocket):
    await _echo_path(websocket)


@api.websocket("/ws/tagged", tags=["Streaming"], summary="Stream prices")
async def ws_tagged(websocket: WebSocket):
    await _echo_path(websocket)


@api.websocket("/ws/route-hidden", include_in_schema=False)
async def ws_route_hidden(websocket: WebSocket):
    await _echo_path(websocket)


internal = BoltAPI(include_in_schema=False)


@internal.get("/secret")
async def secret():
    return {"hidden": "api"}


@internal.get("/shown", include_in_schema=True)
async def shown():
    return {"shown": True}


@internal.websocket("/ws/secret")
async def ws_secret(websocket: WebSocket):
    await _echo_path(websocket)


@internal.websocket("/ws/shown", include_in_schema=True, tags=["Internal"])
async def ws_shown(websocket: WebSocket):
    await _echo_path(websocket)


api.mount("/internal", internal)

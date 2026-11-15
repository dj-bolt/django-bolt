"""App under test for the trailing slash of a WebSocket path over real TCP.

Each `trailing_slash` mode has a route with a trailing slash, a route with no
trailing slash, and a route with a path parameter. Each handler sends its name.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


def _add_routes(target: BoltAPI, label: str) -> None:
    @target.websocket("/ws/slash/")
    async def slash_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(f"{label}:slash")

    @target.websocket("/ws/plain")
    async def plain_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(f"{label}:plain")

    @target.websocket("/ws/room/{name}/")
    async def room_ws(websocket: WebSocket, name: str):
        await websocket.accept()
        await websocket.send_text(f"{label}:room:{name}")


_add_routes(api, "strip")

for _mode in ("append", "keep"):
    _child = BoltAPI(trailing_slash=_mode)
    _add_routes(_child, _mode)
    api.mount(f"/{_mode}", _child)

"""App under test for typed header and cookie values over real TCP.

Each route declares an int header and an int cookie. Valid requests send the
converted values back. A bad value must give HTTP 422, or HTTP 400 at a
WebSocket upgrade.
"""

from __future__ import annotations

from typing import Annotated

from django_bolt import BoltAPI, WebSocket
from django_bolt.params import Cookie, Header

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/typed/async")
async def typed_async(x_count: Annotated[int, Header()], page: Annotated[int, Cookie()] = 1):
    return {"x_count": x_count, "page": page, "types": [type(x_count).__name__, type(page).__name__]}


@api.get("/typed/sync")
def typed_sync(x_count: Annotated[int, Header()], page: Annotated[int, Cookie()] = 1):
    return {"x_count": x_count, "page": page, "types": [type(x_count).__name__, type(page).__name__]}


@api.websocket("/ws/typed")
async def typed_ws(
    websocket: WebSocket,
    x_count: Annotated[int, Header()],
    page: Annotated[int, Cookie()] = 1,
):
    await websocket.accept()
    await websocket.send_text(f"{type(x_count).__name__}:{x_count} {type(page).__name__}:{page}")

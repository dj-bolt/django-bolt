"""WebSocket dispatch to a mounted ASGI application, in process.

Uses `WebSocketTestClient`, which routes through Rust for path matching and
scope construction, then drives the mounted app over a mock transport.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.testing import WebSocketTestClient


async def echo_app(scope, receive, send):
    assert scope["type"] == "websocket"

    connect = await receive()
    assert connect["type"] == "websocket.connect"

    await send({"type": "websocket.accept"})

    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        if "bytes" in message and message["bytes"] is not None:
            await send({"type": "websocket.send", "bytes": message["bytes"]})
        else:
            await send({"type": "websocket.send", "text": f"echo:{message['text']}"})


async def scope_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    headers = {name.decode(): value.decode() for name, value in scope["headers"]}
    await send(
        {
            "type": "websocket.send",
            "text": "|".join(
                [
                    scope["path"],
                    scope["root_path"],
                    scope["query_string"].decode(),
                    headers.get("authorization", ""),
                ]
            ),
        }
    )


def router_app(scope, receive, send):
    """Route like `channels.routing.URLRouter`: strip `root_path`, then match.

    A scope that already removed the prefix makes such a router strip it twice
    and match nothing.
    """
    path = scope["path"]
    root_path = scope["root_path"]
    if root_path and not path.startswith(root_path):
        raise AssertionError(f"path {path!r} must keep the prefix {root_path!r}")
    remainder = path[len(root_path) :].lstrip("/")

    prefix, _, room = remainder.partition("/")
    if prefix != "room" or not room:
        raise AssertionError(f"no route for {remainder!r}")

    async def run():
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": f"room:{room}"})

    return run()


@pytest.fixture
def api():
    api = BoltAPI()

    @api.websocket("/ws/direct")
    async def direct(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("route")

    api.mount_asgi("/mounted", echo_app)
    api.mount_asgi("/scope", scope_app)
    api.mount_asgi("/routed", router_app)
    return api


@pytest.mark.asyncio
async def test_mounted_app_echoes_text(api):
    async with WebSocketTestClient(api, "/mounted/ws") as ws:
        assert ws.accepted
        await ws.send_text("hello")
        assert await ws.receive_text() == "echo:hello"


@pytest.mark.asyncio
async def test_mounted_app_echoes_bytes(api):
    async with WebSocketTestClient(api, "/mounted/ws") as ws:
        await ws.send_bytes(b"\x00\x01")
        assert await ws.receive_bytes() == b"\x00\x01"


@pytest.mark.asyncio
async def test_mounted_app_gets_asgi_scope(api):
    async with WebSocketTestClient(
        api,
        "/scope/inner/path",
        query_string="token=abc",
        headers={"Authorization": "Bearer secret"},
    ) as ws:
        path, root_path, query_string, authorization = (await ws.receive_text()).split("|")

    assert path == "/scope/inner/path"
    assert root_path == "/scope"
    assert query_string == "token=abc"
    assert authorization == "Bearer secret"


@pytest.mark.asyncio
async def test_router_app_reads_url_captures(api):
    async with WebSocketTestClient(api, "/routed/room/lobby") as ws:
        assert await ws.receive_text() == "room:lobby"


@pytest.mark.asyncio
async def test_registered_route_wins_over_mount(api):
    async with WebSocketTestClient(api, "/ws/direct") as ws:
        assert await ws.receive_text() == "route"


@pytest.mark.asyncio
async def test_unmatched_path_still_raises(api):
    with pytest.raises(ValueError, match="No WebSocket handler found"):
        async with WebSocketTestClient(api, "/nowhere"):
            pass

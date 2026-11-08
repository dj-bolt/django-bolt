"""WebSocketTestClient refuses the ASGI messages that the server refuses, with the same errors.

The server side is `create_send_fn` in crates/bolt-websocket/src/handler.rs.
A mounted ASGI app sends raw messages, so these checks reach it directly.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import WebSocketTestClient


async def _error_of(send, message) -> str:
    try:
        await send(message)
    except (RuntimeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return "no error"


async def misuse_app(scope, receive, send):
    await receive()  # websocket.connect
    before_accept = await _error_of(send, {"type": "websocket.send", "text": "too early"})
    await send({"type": "websocket.accept"})
    errors = [
        before_accept,
        await _error_of(send, {"type": "websocket.accept"}),
        await _error_of(send, {"type": "websocket.send"}),
        await _error_of(send, {"type": "websocket.unknown"}),
    ]
    for error in errors:
        await send({"type": "websocket.send", "text": error})
    while (await receive())["type"] != "websocket.disconnect":
        pass


@pytest.mark.asyncio
async def test_the_test_client_refuses_what_the_server_refuses():
    api = BoltAPI()
    api.mount_asgi("/mounted", misuse_app)

    async with WebSocketTestClient(api, "/mounted/ws") as ws:
        errors = [await ws.receive_text() for _ in range(4)]

    assert errors == [
        "RuntimeError: WebSocket is not accepted: call accept() before send",
        "RuntimeError: WebSocket handshake is already complete",
        "ValueError: websocket.send requires 'text' or 'bytes'",
        "ValueError: Unknown message type: websocket.unknown",
    ]

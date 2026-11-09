"""WebSocketTestClient refuses the ASGI messages that the server refuses, with the same errors.

The server side is `create_send_fn` in crates/bolt-websocket/src/handler.rs.
A mounted ASGI app sends raw messages, so these checks reach it directly.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import HandshakeRejected, WebSocketTestClient


async def _error_of(send, message) -> str:
    try:
        await send(message)
    except (ConnectionResetError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc.args[0]}"
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
        await _error_of(send, {"text": "no type"}),
        await _error_of(send, {"type": "websocket.send", "text": None}),
        await _error_of(send, {"type": "websocket.send", "bytes": "not bytes"}),
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
        errors = [await ws.receive_text() for _ in range(7)]

    assert errors == [
        "RuntimeError: WebSocket is not accepted: call accept() before send",
        "RuntimeError: WebSocket handshake is already complete",
        "ValueError: websocket.send requires 'text' or 'bytes'",
        "ValueError: Unknown message type: websocket.unknown",
        "KeyError: Missing 'type' key",
        "TypeError: websocket.send 'text' must be str, not NoneType",
        "TypeError: websocket.send 'bytes' must be bytes, not str",
    ]


@pytest.mark.asyncio
async def test_a_send_after_a_refused_handshake_finds_no_client():
    errors: list[str] = []
    done = asyncio.Event()

    async def refusing_app(scope, receive, send):
        await receive()  # websocket.connect
        await send({"type": "websocket.close"})
        errors.append(await _error_of(send, {"type": "websocket.send", "text": "too late"}))
        done.set()

    api = BoltAPI()
    api.mount_asgi("/mounted", refusing_app)

    with pytest.raises(HandshakeRejected) as rejected:
        async with WebSocketTestClient(api, "/mounted/ws"):
            pass
    await asyncio.wait_for(done.wait(), timeout=5)

    assert rejected.value.status_code == 403
    assert errors == ["ConnectionResetError: WebSocket client disconnected"]

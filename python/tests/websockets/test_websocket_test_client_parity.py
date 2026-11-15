"""WebSocketTestClient refuses the ASGI messages that the server refuses, with the same errors.

The server side is `create_send_fn` in crates/bolt-websocket/src/handler.rs.
A mounted ASGI app sends raw messages, so these checks reach it directly.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import ConnectionClosed, HandshakeRejected, WebSocketTestClient
from tests.integration.apps.websocket_close_frame import api as close_frame_api


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
@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/ws/reason-200-bytes", "WebSocket close reason is 200 bytes: the limit is 123 bytes of UTF-8"),
        # The limit counts bytes, not characters.
        (
            "/ws/reason-124-bytes-62-characters",
            "WebSocket close reason is 124 bytes: the limit is 123 bytes of UTF-8",
        ),
        ("/ws/code-70000", "WebSocket close code 70000 is not valid: use 1000 to 4999, but not 1005, 1006 or 1015"),
        ("/ws/code-1006", "WebSocket close code 1006 is not valid: use 1000 to 4999, but not 1005, 1006 or 1015"),
        # A mounted ASGI app sends the close message with no WebSocket wrapper.
        ("/raw/reason-200-bytes", "WebSocket close reason is 200 bytes: the limit is 123 bytes of UTF-8"),
        ("/raw/code-70000", "WebSocket close code 70000 is not valid: use 1000 to 4999, but not 1005, 1006 or 1015"),
    ],
)
async def test_the_test_client_refuses_the_close_frames_that_the_server_refuses(path, error):
    """The same app runs on the server in test_websocket_close_frame_server_integration.py."""
    async with WebSocketTestClient(close_frame_api, path) as ws:
        assert await ws.receive_text() == f"ValueError: {error}"
        # The connection is still open after the error, so the handler can close it.
        with pytest.raises(ConnectionClosed) as closed:
            await ws.receive_text()
    assert closed.value.code == 1000


@pytest.mark.asyncio
async def test_the_test_client_accepts_a_close_reason_of_123_bytes():
    async with WebSocketTestClient(close_frame_api, "/ws/reason-123-bytes") as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await ws.receive_text()
    assert closed.value.code == 4001


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

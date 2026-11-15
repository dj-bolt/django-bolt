"""The state machine of the ``WebSocket`` wrapper: CONNECTING -> CONNECTED -> DISCONNECTED.

Each test calls a method in the wrong state and checks that the handler gets a clear error.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, WebSocket, WebSocketException, WebSocketState
from django_bolt.testing import ConnectionClosed, HandshakeRejected, WebSocketTestClient
from tests.integration.apps.websocket_frame_types import api as frame_types_api


@pytest.mark.asyncio
async def test_send_before_accept_names_the_missing_accept():
    api = BoltAPI()

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        try:
            await websocket.send_text("too early")
        except RuntimeError as exc:
            error = str(exc)
        await websocket.accept()
        await websocket.send_text(error)
        await websocket.receive_text()

    async with WebSocketTestClient(api, "/ws") as ws:
        assert await ws.receive_text() == "WebSocket is not accepted: call accept() before send()"


@pytest.mark.asyncio
async def test_receive_text_before_accept_names_the_missing_accept():
    api = BoltAPI()

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        try:
            result = repr(await websocket.receive_text())
        except RuntimeError as exc:
            result = str(exc)
        await websocket.accept()
        await websocket.send_text(result)
        await websocket.receive_text()

    async with WebSocketTestClient(api, "/ws") as ws:
        assert await ws.receive_text() == "WebSocket is not accepted: call accept() before receive()"


@pytest.mark.asyncio
async def test_receive_of_the_wrong_frame_type_names_the_frame_and_loses_no_later_frame():
    """The same app runs on the server in test_websocket_frame_types_server_integration.py."""
    async with WebSocketTestClient(frame_types_api, "/ws/text") as ws:
        await ws.send_bytes(b"\x00\x01")
        assert await ws.receive_text() == "1003: receive_text() got a binary frame"
        await ws.send_text("hello")
        assert await ws.receive_text() == "text: hello"

    async with WebSocketTestClient(frame_types_api, "/ws/bytes") as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "1003: receive_bytes() got a text frame"
        await ws.send_bytes(b"\x00\x01")
        assert await ws.receive_text() == "bytes: 0001"


@pytest.mark.asyncio
async def test_receive_json_names_a_frame_of_the_wrong_type():
    api = BoltAPI()
    seen = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        for mode in ("text", "binary"):
            try:
                await websocket.receive_json(mode=mode)
            except WebSocketException as error:
                seen.append((error.code, error.reason))
        await websocket.close()

    async with WebSocketTestClient(api, "/ws") as ws:
        await ws.send_bytes(b"{}")
        await ws.send_text("{}")
        with pytest.raises(ConnectionClosed):
            await ws.receive_text()

    assert seen == [(1003, "receive_text() got a binary frame"), (1003, "receive_bytes() got a text frame")]


@pytest.mark.asyncio
async def test_accept_twice_is_a_no_op():
    api = BoltAPI()

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.accept()
        await websocket.send_text(websocket.state.name)
        await websocket.receive_text()

    async with WebSocketTestClient(api, "/ws") as ws:
        assert await ws.receive_text() == "CONNECTED"


@pytest.mark.asyncio
async def test_accept_after_refusal_does_not_reopen_the_connection():
    api = BoltAPI()
    seen: list[str] = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.close()
        try:
            await websocket.accept()
        except RuntimeError as exc:
            seen.append(str(exc))
        seen.append(websocket.state.name)

    with pytest.raises(HandshakeRejected):
        async with WebSocketTestClient(api, "/ws"):
            pass

    assert seen == ["WebSocket is closed: accept() cannot open it again", "DISCONNECTED"]


@pytest.mark.asyncio
async def test_accept_after_close_raises():
    api = BoltAPI()
    seen: list[str] = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()
        try:
            await websocket.accept()
        except RuntimeError as exc:
            seen.append(str(exc))
        seen.append(websocket.state.name)

    async with WebSocketTestClient(api, "/ws") as ws:
        with pytest.raises(ConnectionClosed):
            await ws.receive_text()

    assert seen == ["WebSocket is closed: accept() cannot open it again", "DISCONNECTED"]


@pytest.mark.asyncio
async def test_send_after_close_says_closed():
    api = BoltAPI()
    seen: list[str] = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()
        try:
            await websocket.send_text("too late")
        except RuntimeError as exc:
            seen.append(str(exc))

    async with WebSocketTestClient(api, "/ws") as ws:
        with pytest.raises(ConnectionClosed):
            await ws.receive_text()

    assert seen == ["WebSocket is closed: send() is not possible"]


def test_state_names_are_unchanged():
    assert [state.name for state in WebSocketState] == ["CONNECTING", "CONNECTED", "DISCONNECTED"]

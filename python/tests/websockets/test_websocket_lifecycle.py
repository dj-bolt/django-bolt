"""The state machine of the ``WebSocket`` wrapper: CONNECTING -> CONNECTED -> DISCONNECTED.

Each test calls a method in the wrong state and checks that the handler gets a clear error.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt import BoltAPI, WebSocket, WebSocketDisconnect, WebSocketState
from django_bolt.testing import ConnectionClosed, HandshakeRejected, WebSocketTestClient
from django_bolt.testing import websocket as websocket_testing


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


@pytest.mark.asyncio
async def test_handler_reads_the_disconnect_when_the_client_leaves():
    """The exit of the client gives the handler a disconnect, and the handler runs to its end."""
    api = BoltAPI()
    seen: list[object] = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        async for message in websocket.iter_text():
            await websocket.send_text(message)
        seen.append("loop ended")
        try:
            await websocket.receive_text()
        except WebSocketDisconnect as exc:
            seen.append(exc.code)
        seen.append(websocket.state.name)

    async with WebSocketTestClient(api, "/ws") as ws:
        await ws.send_text("a")
        assert await ws.receive_text() == "a"

    assert seen == ["loop ended", 1000, "DISCONNECTED"]


@pytest.mark.asyncio
async def test_client_exit_cancels_a_handler_that_does_not_end(monkeypatch):
    monkeypatch.setattr(websocket_testing, "HANDLER_EXIT_TIMEOUT", 0.05)
    api = BoltAPI()
    seen: list[str] = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        try:
            await asyncio.Event().wait()
        finally:
            seen.append("cancelled")

    async with WebSocketTestClient(api, "/ws") as ws:
        assert ws.accepted

    assert seen == ["cancelled"]


def test_state_names_are_unchanged():
    assert [state.name for state in WebSocketState] == ["CONNECTING", "CONNECTED", "DISCONNECTED"]

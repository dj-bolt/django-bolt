"""The state machine of the ``WebSocket`` wrapper: CONNECTING -> CONNECTED -> DISCONNECTED.

Each test calls a method in the wrong state and checks that the handler gets a clear error.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt import BoltAPI, WebSocket, WebSocketDisconnect, WebSocketState
from django_bolt.testing import ConnectionClosed, HandshakeRejected, WebSocketTestClient
from django_bolt.testing import websocket as websocket_testing
from django_bolt.websocket import CloseCode
from tests.integration.apps.websocket_handler_end import api as handler_end_api


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
@pytest.mark.parametrize("path", ["/ws/return", "/raw/return"])
async def test_return_after_accept_closes_the_connection_with_1000(path):
    """The same app runs on the server in test_websocket_handler_end_server_integration.py."""
    async with WebSocketTestClient(handler_end_api, path) as ws:
        # The iterator gives the messages before the close, and then it stops.
        assert [message async for message in ws.iter_text()] == ["bye"]
        assert ws.closed
        assert ws.close_code == 1000


@pytest.mark.asyncio
async def test_close_of_the_handler_is_the_only_close():
    async with WebSocketTestClient(handler_end_api, "/ws/close-then-return") as ws:
        with pytest.raises(ConnectionClosed) as closed:
            await ws.receive_text()
        assert closed.value.code == 4001
        # No second close message is in the queue. A closed client with an empty queue raises at once.
        with pytest.raises(ConnectionClosed) as again:
            await ws.receive(timeout=0.05)
        assert again.value.code == 4001


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
@pytest.mark.parametrize(
    ("iterator_name", "sender_name", "messages"),
    [
        ("iter_text", "send_text", ["one", "two"]),
        ("iter_bytes", "send_bytes", [b"one", b"two"]),
        ("iter_json", "send_json", [{"n": 1}, {"n": 2}]),
    ],
)
async def test_client_iterator_yields_the_messages_sent_before_the_server_close(iterator_name, sender_name, messages):
    api = BoltAPI()

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        for message in messages:
            await getattr(websocket, sender_name)(message)
        await websocket.close()

    async with WebSocketTestClient(api, "/ws") as ws:
        received = [message async for message in getattr(ws, iterator_name)()]
        # The connection is closed and has no more messages. A receive does not wait for the timeout.
        with pytest.raises(ConnectionClosed):
            await ws.receive(timeout=0.01)

    assert received == messages


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("close_args", "expected_code"), [({}, CloseCode.NORMAL), ({"code": 4000}, 4000)])
async def test_client_close_gives_the_handler_a_disconnect_with_the_code(close_args, expected_code):
    api = BoltAPI()
    seen: list[tuple[int, str]] = []

    @api.websocket("/ws")
    async def handler(websocket: WebSocket):
        await websocket.accept()
        try:
            await websocket.receive_text()
        except WebSocketDisconnect as exc:
            seen.append((exc.code, exc.reason))

    async with WebSocketTestClient(api, "/ws") as ws:
        await ws.close(**close_args)
        assert ws.closed
        assert ws.close_code == expected_code
        await ws._handler_task

    assert seen == [(expected_code, "")]

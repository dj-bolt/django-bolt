"""WebSocket subprotocol negotiation through WebSocketTestClient."""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.testing import HandshakeRejected, WebSocketTestClient


def _chat_api() -> BoltAPI:
    api = BoltAPI()

    @api.websocket("/ws/chat")
    async def chat(websocket: WebSocket):
        requested = websocket.subprotocols
        await websocket.accept(subprotocol=requested[0] if requested else None)
        await websocket.send_json({"subprotocols": requested})
        await websocket.receive_text()

    return api


@pytest.mark.asyncio
async def test_subprotocols_argument_reaches_scope_and_is_accepted():
    async with WebSocketTestClient(_chat_api(), "/ws/chat", subprotocols=["chat.v1", "chat.v2"]) as ws:
        data = await ws.receive_json()
        assert data == {"subprotocols": ["chat.v1", "chat.v2"]}
        assert ws.accepted_subprotocol == "chat.v1"


@pytest.mark.asyncio
async def test_subprotocol_header_is_parsed_into_scope():
    async with WebSocketTestClient(
        _chat_api(), "/ws/chat", headers={"Sec-WebSocket-Protocol": " graphql-transport-ws ,graphql-ws"}
    ) as ws:
        data = await ws.receive_json()
        assert data["subprotocols"] == ["graphql-transport-ws", "graphql-ws"]
        assert ws.accepted_subprotocol == "graphql-transport-ws"


@pytest.mark.asyncio
async def test_no_subprotocol_requested_gives_empty_list():
    async with WebSocketTestClient(_chat_api(), "/ws/chat") as ws:
        data = await ws.receive_json()
        assert data == {"subprotocols": []}
        assert ws.accepted_subprotocol is None


@pytest.mark.asyncio
async def test_accepting_unrequested_subprotocol_fails():
    api = BoltAPI()

    @api.websocket("/ws/bad")
    async def bad(websocket: WebSocket):
        await websocket.accept(subprotocol="not-requested")

    with pytest.raises(ValueError, match="not-requested"):
        async with WebSocketTestClient(api, "/ws/bad", subprotocols=["chat.v1"]):
            pass


@pytest.mark.asyncio
async def test_close_before_accept_rejects_handshake_with_403():
    api = BoltAPI()

    @api.websocket("/ws/refuse")
    async def refuse(websocket: WebSocket):
        await websocket.close()

    with pytest.raises(HandshakeRejected) as rejected:
        async with WebSocketTestClient(api, "/ws/refuse"):
            pass
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_return_before_accept_rejects_handshake_with_403():
    api = BoltAPI()

    @api.websocket("/ws/no-accept")
    async def no_accept(websocket: WebSocket):
        return

    with pytest.raises(HandshakeRejected) as rejected:
        async with WebSocketTestClient(api, "/ws/no-accept"):
            pass
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_error_before_accept_raises_the_handler_error():
    api = BoltAPI()

    @api.websocket("/ws/fail")
    async def fail(websocket: WebSocket):
        raise RuntimeError("boom before accept")

    with pytest.raises(RuntimeError, match="boom before accept"):
        async with WebSocketTestClient(api, "/ws/fail"):
            pass


@pytest.mark.asyncio
async def test_receive_before_accept_gives_connect_event():
    api = BoltAPI()

    @api.websocket("/ws/connect-first")
    async def connect_first(websocket: WebSocket):
        message = await websocket.receive()
        await websocket.accept()
        await websocket.send_text(message["type"])
        await websocket.receive_text()

    async with WebSocketTestClient(api, "/ws/connect-first") as ws:
        assert await ws.receive_text() == "websocket.connect"


def _mounted_api() -> BoltAPI:
    api = BoltAPI()

    async def app(scope, receive, send):
        assert (await receive())["type"] == "websocket.connect"
        if scope["path"].endswith("/refuse"):
            await send({"type": "websocket.close", "code": 1000})
            return
        subprotocols = scope["subprotocols"]
        await send({"type": "websocket.accept", "subprotocol": subprotocols[-1]})
        await send({"type": "websocket.send", "text": ",".join(subprotocols)})
        while (await receive())["type"] != "websocket.disconnect":
            pass

    api.mount_asgi("/mounted", app)
    return api


@pytest.mark.asyncio
async def test_mounted_asgi_app_gets_subprotocols_and_accepts_one():
    async with WebSocketTestClient(_mounted_api(), "/mounted/chat", subprotocols=["chat.v1", "chat.v2"]) as ws:
        assert await ws.receive_text() == "chat.v1,chat.v2"
        assert ws.accepted_subprotocol == "chat.v2"


@pytest.mark.asyncio
async def test_mounted_asgi_app_close_before_accept_rejects_handshake():
    with pytest.raises(HandshakeRejected) as rejected:
        async with WebSocketTestClient(_mounted_api(), "/mounted/refuse"):
            pass
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", [b"sec-websocket-protocol", b"Sec-WebSocket-Accept", b"sec-websocket-extensions", b"upgrade", b"connection"]
)
async def test_accept_rejects_headers_owned_by_the_handshake(name):
    api = BoltAPI()

    @api.websocket("/ws/owned")
    async def owned(websocket: WebSocket):
        await websocket.accept(subprotocol="chat.v1", headers=[(name, b"other")])

    with pytest.raises(ValueError, match="handshake"):
        async with WebSocketTestClient(api, "/ws/owned", subprotocols=["chat.v1"]):
            pass

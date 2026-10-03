"""WebSocket subprotocol negotiation through WebSocketTestClient."""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.testing import ConnectionClosed, WebSocketTestClient
from django_bolt.websocket import CloseCode


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
        async with WebSocketTestClient(api, "/ws/bad", subprotocols=["chat.v1"]) as ws:
            with pytest.raises(ConnectionClosed):
                await ws.receive_text()
            assert not ws.accepted
            assert ws.close_code == CloseCode.INTERNAL_ERROR

from __future__ import annotations

import socket
import time

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


@pytest.fixture
def server(make_server_project):
    with make_server_project(api_module=app_module("websocket_subprotocol")).start() as running:
        yield running


def test_accept_puts_selected_subprotocol_in_101_response(server):
    with SimpleWebSocketClient(
        server.host,
        server.port,
        "/ws/chat",
        headers={"Sec-WebSocket-Protocol": "chat.v1, chat.v2"},
    ) as websocket:
        assert websocket.response_headers["sec-websocket-protocol"] == "chat.v2"
        assert websocket.receive_text() == "chat.v1,chat.v2"


def test_no_subprotocol_requested_sends_no_protocol_header(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/chat") as websocket:
        assert "sec-websocket-protocol" not in websocket.response_headers
        assert websocket.receive_text() == ""


def test_accept_headers_are_added_to_101_response(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/headers") as websocket:
        assert websocket.response_headers["x-session"] == "abc123"
        assert websocket.receive_text() == "ok"


def test_close_before_accept_refuses_handshake_with_403(server):
    status_line, _body = attempt_ws_upgrade(server.host, server.port, "/ws/refuse")
    assert " 403 " in status_line


def test_return_before_accept_refuses_handshake_with_403(server):
    status_line, _body = attempt_ws_upgrade(server.host, server.port, "/ws/no-accept")
    assert " 403 " in status_line


def test_error_before_accept_refuses_handshake_with_500(server):
    status_line, _body = attempt_ws_upgrade(server.host, server.port, "/ws/fail")
    assert " 500 " in status_line


def test_unrequested_subprotocol_fails_handshake(server):
    status_line, _body = attempt_ws_upgrade(
        server.host,
        server.port,
        "/ws/unrequested",
        headers={"Sec-WebSocket-Protocol": "chat.v1"},
    )
    assert " 500 " in status_line


def test_subprotocols_split_over_header_lines_are_negotiated(server):
    with SimpleWebSocketClient(
        server.host,
        server.port,
        "/ws/chat",
        headers={"Sec-WebSocket-Protocol": "chat.v1\r\nSec-WebSocket-Protocol: chat.v2"},
    ) as websocket:
        assert websocket.response_headers["sec-websocket-protocol"] == "chat.v2"
        assert websocket.receive_text() == "chat.v1,chat.v2"


def test_receive_before_accept_gives_connect_event(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/connect-first") as websocket:
        assert websocket.receive_text() == "websocket.connect"


def test_send_after_client_left_during_handshake_raises_disconnect(server):
    # Send the upgrade request and leave before the handler accepts.
    with socket.create_connection((server.host, server.port), timeout=5) as sock:
        sock.sendall(
            (
                "GET /ws/slow-accept HTTP/1.1\r\n"
                f"Host: {server.host}:{server.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )

    deadline = time.monotonic() + 5
    errors: list[str] = []
    while not errors and time.monotonic() < deadline:
        errors = server.get("/send-after-leave").json()["errors"]
        time.sleep(0.1)
    assert errors == ["WebSocketDisconnect"]


def test_mounted_asgi_app_negotiates_subprotocol_and_headers(server):
    with SimpleWebSocketClient(
        server.host,
        server.port,
        "/mounted/chat",
        headers={"Sec-WebSocket-Protocol": "graphql-transport-ws, graphql-ws"},
    ) as websocket:
        assert websocket.response_headers["sec-websocket-protocol"] == "graphql-transport-ws"
        assert websocket.response_headers["x-mounted"] == "yes"
        assert websocket.receive_text() == "graphql-transport-ws,graphql-ws"


def test_mounted_asgi_app_close_before_accept_refuses_with_403(server):
    status_line, _body = attempt_ws_upgrade(server.host, server.port, "/mounted/refuse")
    assert " 403 " in status_line

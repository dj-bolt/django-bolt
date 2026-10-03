from __future__ import annotations

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

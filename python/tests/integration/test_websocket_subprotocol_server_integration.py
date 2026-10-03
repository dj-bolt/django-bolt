from __future__ import annotations

import socket
import time

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


def _upgrade_request(host: str, port: int, path: str, *, version: str = "13") -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: {version}\r\n\r\n"
    ).encode()


def _upgrade_head(server, path: str, *, version: str = "13") -> tuple[str, dict[str, str]]:
    """Send an upgrade request and return the status line and lowercase headers."""
    with socket.create_connection((server.host, server.port), timeout=5) as sock:
        sock.sendall(_upgrade_request(server.host, server.port, path, version=version))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    lines = response.split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


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
    status_line, headers = _upgrade_head(server, "/ws/refuse")
    assert " 403 " in status_line
    # A refusal is a plain HTTP response, with no upgrade headers.
    assert "upgrade" not in headers
    assert "sec-websocket-accept" not in headers


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
        sock.sendall(_upgrade_request(server.host, server.port, "/ws/slow-accept"))

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


def test_accept_header_owned_by_the_handshake_fails_handshake(server):
    status_line, _body = attempt_ws_upgrade(
        server.host,
        server.port,
        "/ws/owned-header",
        headers={"Sec-WebSocket-Protocol": "chat.v1"},
    )
    assert " 500 " in status_line


def test_mounted_asgi_app_error_before_accept_fails_with_500(server):
    status_line, _body = attempt_ws_upgrade(server.host, server.port, "/mounted/boom")
    assert " 500 " in status_line


def test_mounted_asgi_app_unrequested_subprotocol_fails_with_500(server):
    status_line, _body = attempt_ws_upgrade(
        server.host,
        server.port,
        "/mounted/unrequested",
        headers={"Sec-WebSocket-Protocol": "chat.v1"},
    )
    assert " 500 " in status_line


def test_bad_handshake_is_rejected_before_the_handler_runs(server):
    status_line, _headers = _upgrade_head(server, "/ws/version-probe", version="99")
    assert " 400 " in status_line

    # A good handshake reaches the handler, so the probe counts calls.
    status_line, _headers = _upgrade_head(server, "/ws/version-probe")
    assert " 101 " in status_line
    assert server.get("/version-probe-calls").json() == {"calls": 1}


def test_refused_and_abandoned_handshakes_release_connection_slots(make_server_project):
    project = make_server_project(
        api_module=app_module("websocket_subprotocol"),
        settings_extra="BOLT_WS_MAX_CONNECTIONS = 2\n",
    )
    with project.start() as server:
        # Refusals and errors before accept hold no slot after the response.
        for path in ("/ws/refuse", "/ws/no-accept", "/ws/fail", "/mounted/refuse", "/mounted/boom"):
            status_line, _body = attempt_ws_upgrade(server.host, server.port, path)
            assert " 101 " not in status_line, path

        # Clients that leave during the handshake release their slots too.
        for _ in range(2):
            with socket.create_connection((server.host, server.port), timeout=5) as sock:
                sock.sendall(_upgrade_request(server.host, server.port, "/ws/slow-accept"))
        deadline = time.monotonic() + 5
        while len(server.get("/send-after-leave").json()["errors"]) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)

        # Both slots are free: two connections open, and a third gets 503.
        with (
            SimpleWebSocketClient(server.host, server.port, "/ws/chat") as first,
            SimpleWebSocketClient(server.host, server.port, "/ws/chat"),
        ):
            first.receive_text()
            status_line, _body = attempt_ws_upgrade(server.host, server.port, "/ws/chat")
            assert " 503 " in status_line

        # A closed connection frees its slot.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status_line, _headers = _upgrade_head(server, "/ws/chat")
            if " 101 " in status_line:
                break
            time.sleep(0.1)
        assert " 101 " in status_line

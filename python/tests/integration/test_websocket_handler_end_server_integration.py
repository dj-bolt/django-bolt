from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


@pytest.fixture
def server(make_server_project):
    with make_server_project(api_module=app_module("websocket_handler_end")).start() as running:
        yield running


# A mounted ASGI app that returns gets the same close as a route.
@pytest.mark.parametrize("path", ["/ws/return", "/raw/return"])
def test_return_after_accept_closes_the_connection_with_1000(server, path):
    with SimpleWebSocketClient(server.host, server.port, path) as websocket:
        assert websocket.receive_text() == "bye"
        assert websocket.receive_close() == (1000, "")


def test_error_after_accept_closes_the_connection_with_1011(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/raise") as websocket:
        assert websocket.receive_text() == "bye"
        assert websocket.receive_close() == (1011, "Internal error")


def test_websocket_exception_closes_the_connection_with_its_code_and_reason(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/exception") as websocket:
        assert websocket.receive_text() == "bye"
        assert websocket.receive_close() == (4001, "nope")
    with SimpleWebSocketClient(server.host, server.port, "/ws/close-exception") as websocket:
        assert websocket.receive_close() == (4002, "done")


@pytest.mark.parametrize("path", ["/ws/invalid-code", "/ws/invalid-reason-set-later"])
def test_websocket_exception_that_makes_an_invalid_close_frame_is_a_handler_error(server, path):
    """The client gets a valid close frame: 1011, as for each error of the handler."""
    with SimpleWebSocketClient(server.host, server.port, path) as websocket:
        assert websocket.receive_close() == (1011, "Internal error")


def test_websocket_exception_before_accept_refuses_the_handshake_with_403(server):
    status_line, _body = attempt_ws_upgrade(server.host, server.port, "/ws/exception-before-accept")
    assert status_line == "HTTP/1.1 403 Forbidden"


def test_close_of_the_handler_is_the_only_close_frame(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/close-then-return") as websocket:
        assert websocket.receive_close() == (4001, "own close")
        # The server sends no second close frame. It closes the TCP connection.
        with pytest.raises(AssertionError, match="connection closed while reading a frame"):
            websocket.receive_close()

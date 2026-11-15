from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

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


def test_close_of_the_handler_is_the_only_close_frame(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/close-then-return") as websocket:
        assert websocket.receive_close() == (4001, "own close")
        # The server sends no second close frame. It closes the TCP connection.
        with pytest.raises(AssertionError, match="connection closed while reading a frame"):
            websocket.receive_close()

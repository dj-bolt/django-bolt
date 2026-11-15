from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration

REASON_ERROR = "ValueError: WebSocket close reason is {} bytes: the limit is 123 bytes of UTF-8"
CODE_ERROR = "ValueError: WebSocket close code {} is not valid: use 1000 to 4999, but not 1005, 1006 or 1015"


@pytest.fixture
def server(make_server_project):
    with make_server_project(api_module=app_module("websocket_close_frame")).start() as running:
        yield running


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/ws/reason-200-bytes", REASON_ERROR.format(200)),
        # The limit counts bytes, not characters.
        ("/ws/reason-124-bytes-62-characters", REASON_ERROR.format(124)),
        ("/ws/code-70000", CODE_ERROR.format(70000)),
        ("/ws/code-1006", CODE_ERROR.format(1006)),
        # A mounted ASGI app sends the close message with no WebSocket wrapper.
        ("/raw/reason-200-bytes", REASON_ERROR.format(200)),
        ("/raw/code-70000", CODE_ERROR.format(70000)),
    ],
)
def test_close_that_makes_an_invalid_frame_raises_value_error(server, path, error):
    """RFC 6455 5.5: the payload of a close frame is at most 125 bytes."""
    with SimpleWebSocketClient(server.host, server.port, path) as websocket:
        assert websocket.receive_text() == error
        # The connection is still open after the error, so the handler can close it.
        assert websocket.receive_close() == (1000, "refused")


def test_close_sends_a_reason_of_123_bytes(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/reason-123-bytes") as websocket:
        assert websocket.receive_close() == (4001, "é" * 61 + "x")

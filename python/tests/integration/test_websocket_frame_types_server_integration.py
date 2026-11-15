from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


@pytest.fixture
def server(make_server_project):
    with make_server_project(api_module=app_module("websocket_frame_types")).start() as running:
        yield running


def test_receive_text_names_a_binary_frame_and_reads_the_next_text_frame(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/text") as websocket:
        websocket.send_bytes(b"\x00\x01")
        assert websocket.receive_text() == "1003: receive_text() got a binary frame"
        websocket.send_text("hello")
        assert websocket.receive_text() == "text: hello"


def test_receive_bytes_names_a_text_frame_and_reads_the_next_binary_frame(server):
    with SimpleWebSocketClient(server.host, server.port, "/ws/bytes") as websocket:
        websocket.send_text("hello")
        assert websocket.receive_text() == "1003: receive_bytes() got a text frame"
        websocket.send_bytes(b"\x00\x01")
        assert websocket.receive_text() == "bytes: 0001"

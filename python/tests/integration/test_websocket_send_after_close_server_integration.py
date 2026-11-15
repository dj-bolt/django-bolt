from __future__ import annotations

import time

import httpx
import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_a_send_after_the_close_of_the_app_finds_no_client(make_server_project):
    """WebSocketTestClient gives the same error in test_websocket_test_client_parity.py."""
    with make_server_project(api_module=app_module("websocket_send_after_close")).start() as server:
        with SimpleWebSocketClient(server.host, server.port, "/raw/ws") as websocket:
            assert websocket.receive_close() == (4000, "")
        deadline = time.monotonic() + 5
        result = httpx.get(server.url("/result")).json()
        while not result and time.monotonic() < deadline:
            time.sleep(0.05)
            result = httpx.get(server.url("/result")).json()

    assert result == {"send": "ConnectionResetError: WebSocket client disconnected"}

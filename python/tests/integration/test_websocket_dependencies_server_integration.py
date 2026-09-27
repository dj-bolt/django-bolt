"""The dependencies of a WebSocket handler through the production handshake.

`WebSocketTestClient` reaches `src/testing.rs` only. This test runs `runbolt`
and reaches the production WebSocket `build_scope`.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_runbolt_websocket_dependency_gets_its_query_and_header_values(make_server_project):
    project = make_server_project(api_module=app_module("websocket_dependencies"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws?limit=3", headers={"X-Team": "red"}) as websocket,
    ):
        message = json.loads(websocket.receive_text())

    assert message == {"limit": 3, "limit_type": "int", "team": "red"}

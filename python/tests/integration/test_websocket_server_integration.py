from __future__ import annotations

import pytest

from .apps import app_source
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def _make_websocket_project(make_server_project):
    return make_server_project(api_source=app_source("websocket_auth"))


def test_websocket_handshake_auth_and_close(make_server_project):
    project = _make_websocket_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host,
            server.port,
            "/ws/protected",
            headers={"Authorization": "Bearer secret-token"},
        ) as websocket,
    ):
        assert websocket.receive_text() == "authorized"
        websocket.send_text("hello")
        assert websocket.receive_text() == "echo:hello"
        websocket.send_text("bye")
        code, reason = websocket.receive_close()

    assert code == 1000
    assert reason == "done"


def test_websocket_rejects_missing_auth(make_server_project):
    project = _make_websocket_project(make_server_project)

    with project.start() as server, SimpleWebSocketClient(server.host, server.port, "/ws/protected") as websocket:
        code, reason = websocket.receive_close()

    assert code == 1008
    assert reason == "unauthorized"

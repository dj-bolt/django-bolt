from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import ServerProject, SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def _make_project(make_server_project) -> ServerProject:
    return make_server_project(api_module=app_module("ws_asgi_mount"))


def test_mounted_asgi_app_serves_websocket(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/mounted/ws") as websocket,
    ):
        websocket.receive_text()
        websocket.send_text("hello")
        assert websocket.receive_text() == "echo:hello"
        websocket.send_text("bye")
        code, reason = websocket.receive_close()

    assert code == 1000
    assert reason == "done"


def test_mounted_asgi_websocket_scope_follows_spec(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host,
            server.port,
            "/mounted/ws?token=abc",
            headers={"Authorization": "Bearer secret-token"},
        ) as websocket,
    ):
        scope = json.loads(websocket.receive_text())

    # ASGI keeps the mount prefix on the path. A router strips root_path itself.
    assert scope["path"] == "/mounted/ws"
    assert scope["root_path"] == "/mounted"
    assert scope["query_string"] == "token=abc"
    assert scope["authorization"] == "Bearer secret-token"


def test_registered_route_still_wins_over_mounts(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/direct") as websocket,
    ):
        assert websocket.receive_text() == "route"

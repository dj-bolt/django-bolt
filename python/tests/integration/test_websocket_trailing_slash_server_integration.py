"""Integration tests for the trailing slash of a WebSocket path.

The server finds a WebSocket route in Actix first, then in its WebSocket router.
The in-process `WebSocketTestClient` has no Actix step. Thus only a real
handshake against `runbolt` shows that both forms of a path connect.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

PATHS = [
    (f"{prefix}{path}", f"{mode}:{message}")
    for mode, prefix in (("strip", ""), ("append", "/append"), ("keep", "/keep"))
    for path, message in (
        ("/ws/slash", "slash"),
        ("/ws/slash/", "slash"),
        ("/ws/plain", "plain"),
        ("/ws/plain/", "plain"),
        ("/ws/room/blue", "room:blue"),
        ("/ws/room/blue/", "room:blue"),
    )
]


@pytest.mark.server_integration
def test_websocket_route_connects_with_and_without_a_trailing_slash(make_server_project):
    """Both forms of a WebSocket path connect, for each `trailing_slash` mode."""
    project = make_server_project(api_module=app_module("ws_trailing_slash"))

    answers = {}
    with project.start() as server:
        for path, _ in PATHS:
            try:
                with SimpleWebSocketClient(server.host, server.port, path) as websocket:
                    answers[path] = websocket.receive_text()
            except Exception as exc:  # noqa: BLE001 - the test reports each refused path
                answers[path] = f"refused: {exc!r}"

    assert answers == dict(PATHS)

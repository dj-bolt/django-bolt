"""The decode of `+` in a query through the production handlers.

`TestClient` reaches `src/testing.rs` only. These tests run `runbolt` and
reach `src/handler.rs` and the production WebSocket `build_scope`.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_runbolt_decodes_plus_in_query_as_space(make_server_project):
    project = make_server_project(api_module=app_module("query_plus"))

    with project.start() as server:
        plus = server.get("/search?q=hello+world%2B1")
        params = server.get("/search", params={"q": "x y"})
        path = server.get("/items/a+b")

    assert plus.status_code == 200, plus.text
    assert plus.json() == {"q": "hello world+1"}
    assert params.status_code == 200, params.text
    assert params.json() == {"q": "x y"}
    assert path.status_code == 200, path.text
    assert path.json() == {"name": "a+b"}


def test_runbolt_websocket_decodes_plus_in_query_as_space(make_server_project):
    project = make_server_project(api_module=app_module("query_plus"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/search?q=hello+world%2B1") as websocket,
    ):
        message = json.loads(websocket.receive_text())

    assert message == {"q": "hello world+1"}

"""Type conversion by wire name through the production handlers.

`TestClient` reaches `src/testing.rs` only. These tests run `runbolt` and
reach `src/handler.rs` and the production WebSocket `build_scope`.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_runbolt_converts_parameters_by_wire_name(make_server_project):
    project = make_server_project(api_module=app_module("param_wire_names"))

    with project.start() as server:
        query_ok = server.get("/query?p=3")
        query_bad = server.get("/query?p=abc")
        path_ok = server.get("/items/5")
        path_bad = server.get("/items/abc")
        header_ok = server.get("/header", headers={"x-count": "4"})
        header_bad = server.get("/header", headers={"x-count": "abc"})
        cookie_ok = server.get("/cookie", headers={"cookie": "n=7"})
        cookie_bad = server.get("/cookie", headers={"cookie": "n=abc"})

    assert query_ok.status_code == 200, query_ok.text
    assert query_ok.json() == {"page": 3, "type": "int"}
    assert query_bad.status_code == 422, query_bad.text
    assert path_ok.status_code == 200, path_ok.text
    assert path_ok.json() == {"item_id": 5, "type": "int"}
    assert path_bad.status_code == 422, path_bad.text
    assert header_ok.status_code == 200, header_ok.text
    assert header_ok.json() == {"x_count": 4, "type": "int"}
    assert header_bad.status_code == 422, header_bad.text
    assert cookie_ok.status_code == 200, cookie_ok.text
    assert cookie_ok.json() == {"count": 7, "type": "int"}
    assert cookie_bad.status_code == 422, cookie_bad.text


def test_runbolt_websocket_converts_parameters_by_wire_name(make_server_project):
    project = make_server_project(api_module=app_module("param_wire_names"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/rooms/9?p=3") as websocket,
    ):
        message = json.loads(websocket.receive_text())

    assert message == {"room_id": "int", "page": "int"}

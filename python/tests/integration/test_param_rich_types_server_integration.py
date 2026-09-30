"""Sequence, NewType and constrained parameters through the production handler.

`TestClient` reaches `src/testing.rs` only. These tests run `runbolt` and
reach `src/handler.rs` and `build_scope()` of `bolt-websocket`, which collect
each value of a repeated query key.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_runbolt_converts_sequence_newtype_and_constrained_parameters(make_server_project):
    project = make_server_project(api_module=app_module("param_rich_types"))

    with project.start() as server:
        tags = server.get("/tags?tag=3&tag=1&tag=3")
        bad_tag = server.get("/tags?tag=1&tag=x")
        user = server.get("/users/7?page=2")
        bad_user = server.get("/users/x")
        bad_page = server.get("/users/7?page=0")

    assert tags.status_code == 200, tags.text
    assert tags.json() == {"tag": [1, 3], "type": "set"}
    assert bad_tag.status_code == 422, bad_tag.text
    assert user.status_code == 200, user.text
    assert user.json() == {"user_id": 7, "type": "int", "page": 2}
    assert bad_user.status_code == 422, bad_user.text
    assert bad_page.status_code == 422, bad_page.text


def test_runbolt_gives_a_websocket_sequence_parameter_each_value(make_server_project):
    project = make_server_project(api_module=app_module("param_rich_types"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/tags?tag=3&tag=1&tag=3") as websocket,
    ):
        response = json.loads(websocket.receive_text())

    assert response == {"tag": [3, 1, 3]}

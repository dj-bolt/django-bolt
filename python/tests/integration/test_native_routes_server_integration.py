"""runbolt registers native routes with the server."""

from __future__ import annotations

import pytest

from .apps import app_module


@pytest.mark.server_integration
def test_runbolt_serves_a_native_route(make_server_project):
    project = make_server_project(api_module=app_module("native_routes"))
    with project.start(startup_path="/health") as server:
        response = server.request("GET", "/native/bolt")

    assert response.status_code == 201
    assert response.json()["params"] == {"id": "bolt"}

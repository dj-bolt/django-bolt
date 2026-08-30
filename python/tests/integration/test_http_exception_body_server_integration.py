"""HTTPException.body over a real ``runbolt`` server (async and sync dispatch)."""

from __future__ import annotations

import pytest

from .apps import app_module


@pytest.mark.server_integration
def test_http_exception_body_end_to_end(make_server_project):
    project = make_server_project(api_module=app_module("http_exception_body"))
    with project.start(startup_path="/health") as server:
        typed = server.request("POST", "/plans/0/start")
        subclass = server.request("POST", "/plans/1/start")
        plain = server.request("POST", "/plans/2/start")
        ok = server.request("POST", "/plans/3/start")
        sync_typed = server.request("POST", "/plans-sync/0/start")

    assert typed.status_code == 400
    assert typed.json() == {"error": "not healthy", "code": 1}
    assert typed.headers["x-reason"] == "health"
    assert typed.headers["content-type"].startswith("application/json")

    assert subclass.status_code == 404
    assert subclass.json() == {"error": "missing", "code": 7}

    assert plain.status_code == 400
    assert plain.json() == {"detail": "plain"}

    assert ok.status_code == 200
    assert ok.json() == {"id": 3}

    assert sync_typed.status_code == 400
    assert sync_typed.json() == {"error": "not healthy", "code": 1}

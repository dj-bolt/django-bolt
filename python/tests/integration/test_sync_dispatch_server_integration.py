"""Two-altitude coverage for the sync-dispatch bypass + bare-bytes wire.

The in-process test pins behavior through the async dispatch (TestClient);
the subprocess test drives the SAME app module through a real ``runbolt``
server where ``can_sync_dispatch`` routes take Rust's sync-dispatch branch —
including the bare-bytes JSON response wire and Rust-prebound optional params.
Both altitudes must agree on every payload.
"""

from __future__ import annotations

import pytest

from django_bolt.testing import TestClient

from .apps import app_module
from .apps import sync_dispatch as sync_dispatch_app

EXPECTATIONS = [
    ("/plain-json", 200, {"message": "sync-dispatch", "n": 1}),
    ("/created", 201, {"created": True}),
    ("/items/7?q=hello&limit=3", 200, {"id": 7, "q": "hello", "limit": 3}),
    # q omitted → function default; limit omitted → None
    ("/items/9", 200, {"id": 9, "q": "none", "limit": None}),
    # Optional annotation, no function default: present and missing (inject-None)
    ("/search?term=abc", 200, {"term": "abc"}),
    ("/search", 200, {"term": None}),
    ("/strict?token=abc", 200, {"token": "abc"}),
    # `async for` (no `await` statement) must not be misclassified as
    # trivially-async: its suspensions would 500 under the sync executor.
    ("/aiter", 200, {"total": 6}),
]


def test_sync_dispatch_routes_in_process():
    with TestClient(sync_dispatch_app.api) as client:
        for path, status, payload in EXPECTATIONS:
            response = client.get(path)
            assert response.status_code == status, path
            assert response.json() == payload, path

        text = client.get("/text")
        assert text.status_code == 200
        assert text.text == "hello-sync"
        assert text.headers["content-type"].startswith("text/plain")

        missing = client.get("/strict")
        assert missing.status_code == 422


@pytest.mark.server_integration
def test_sync_dispatch_routes_through_real_server(make_server_project):
    project = make_server_project(api_module=app_module("sync_dispatch"))

    with project.start() as server:
        for path, status, payload in EXPECTATIONS:
            response = server.get(path)
            assert response.status_code == status, path
            assert response.json() == payload, path
            if status == 200 and path != "/text":
                assert response.headers["content-type"].startswith("application/json"), path

        text = server.get("/text")
        assert text.status_code == 200
        assert text.text == "hello-sync"
        assert text.headers["content-type"].startswith("text/plain")

        # Missing required path-param type coercion still 422s (injector
        # fallback preserved even with Rust prebinding).
        bad = server.get("/items/not-an-int")
        assert bad.status_code == 422

        # Required query param present vs missing: the missing case makes Rust
        # prebinding bail so the Python injector raises the structured 422.
        ok = server.get("/strict?token=abc")
        assert ok.status_code == 200
        assert ok.json() == {"token": "abc"}
        missing = server.get("/strict")
        assert missing.status_code == 422

"""Integration tests for typed header and cookie values over real TCP.

These exercise the production request path in `src/handler.rs` (async and
sync dispatch) and the production WebSocket upgrade in
`crates/bolt-websocket/src/handler.rs::build_scope`. The in-process clients
reach only `src/testing.rs`.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


@pytest.mark.parametrize("path", ["/typed/async", "/typed/sync"])
def test_http_typed_header_and_cookie(make_server_project, path):
    """Typed values arrive as ints, and bad values give 422 naming the source."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        response = server.get(path, headers={"X-Count": "5", "Cookie": "page=3"})
        assert response.status_code == 200, response.text
        assert response.json() == {"x_count": 5, "page": 3, "types": ["int", "int"]}

        response = server.get(path, headers={"X-Count": "abc"})
        assert response.status_code == 422, response.text
        assert "Header 'x-count'" in response.json()["detail"]

        response = server.get(path, headers={"X-Count": "1", "Cookie": "page=last"})
        assert response.status_code == 422, response.text
        assert "Cookie 'page'" in response.json()["detail"]


def test_websocket_typed_header_and_cookie(make_server_project):
    """Typed values arrive as ints, and bad values reject the upgrade with 400."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        headers = {"X-Count": "5", "Cookie": "page=3"}
        with SimpleWebSocketClient(server.host, server.port, "/ws/typed", headers=headers) as websocket:
            assert websocket.receive_text() == "int:5 int:3"

        status_line, body = attempt_ws_upgrade(server.host, server.port, "/ws/typed", headers={"X-Count": "abc"})
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Header 'x-count'" in body, f"body={body!r}"

        status_line, body = attempt_ws_upgrade(
            server.host, server.port, "/ws/typed", headers={"X-Count": "1", "Cookie": "page=last"}
        )
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Cookie 'page'" in body, f"body={body!r}"

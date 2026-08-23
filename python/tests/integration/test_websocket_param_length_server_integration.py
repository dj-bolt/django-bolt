"""Integration tests for WebSocket parameter length enforcement.

These exercise the *production* WebSocket upgrade path in
`src/websocket/handler.rs::build_scope` over a real TCP handshake against a
`runbolt` server. The in-process `WebSocketTestClient` tests only reach
`src/testing.rs::handle_test_websocket`, so this is the only coverage for the
production builder rejecting oversized path/query params with an HTTP 400 instead
of passing the raw string through to Python.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


def _make_ws_project(make_server_project):
    return make_server_project(api_module=app_module("ws_param_length"))


def test_websocket_oversized_path_param_rejects_upgrade(make_server_project):
    """An oversized path param rejects the upgrade with 400 instead of upgrading."""
    project = _make_ws_project(make_server_project)
    oversized = "a" * 9000  # exceeds the default 8192-byte limit

    with project.start() as server:
        status_line, body = attempt_ws_upgrade(server.host, server.port, f"/ws/path/{oversized}")

    assert "400" in status_line, f"Expected 400 rejection, got status={status_line!r} body={body!r}"
    assert "Parameter too long" in body, f"body={body!r}"


def test_websocket_oversized_query_param_rejects_upgrade(make_server_project):
    """An oversized query param rejects the upgrade with 400 instead of upgrading."""
    project = _make_ws_project(make_server_project)
    oversized = "a" * 9000

    with project.start() as server:
        status_line, body = attempt_ws_upgrade(server.host, server.port, f"/ws/plain?value={oversized}")

    assert "400" in status_line, f"Expected 400 rejection, got status={status_line!r} body={body!r}"
    assert "Parameter too long" in body, f"body={body!r}"


def test_websocket_normal_path_param_completes_handshake(make_server_project):
    """Positive control: a normal-sized path param still upgrades and runs the handler."""
    project = _make_ws_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/path/hello") as websocket,
    ):
        assert websocket.receive_text() == "connected"


def test_websocket_honors_django_bolt_max_param_length_env(make_server_project):
    """The upgrade limit is driven by DJANGO_BOLT_MAX_PARAM_LENGTH, not hard-coded to 8192.

    Exercises the production startup path with the env override set: a value that
    would be rejected under the default limit now completes the handshake, while a
    value over the *raised* limit is still rejected before the upgrade.
    """
    project = _make_ws_project(make_server_project)

    accepted = "a" * 10000  # over the default 8192, under the configured 16384
    rejected = "a" * 16385  # over the configured 16384

    with project.start(env={"DJANGO_BOLT_MAX_PARAM_LENGTH": "16384"}) as server:
        status_line, body = attempt_ws_upgrade(server.host, server.port, f"/ws/path/{rejected}")
        assert "400" in status_line, f"Expected 400 rejection, got status={status_line!r} body={body!r}"
        assert "Parameter too long" in body, f"body={body!r}"

        with SimpleWebSocketClient(server.host, server.port, f"/ws/path/{accepted}") as websocket:
            assert websocket.receive_text() == "connected"

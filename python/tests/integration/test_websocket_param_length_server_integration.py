"""Integration tests for WebSocket parameter length enforcement.

These exercise the *production* WebSocket upgrade path in
`src/websocket/handler.rs::build_scope` over a real TCP handshake against a
`runbolt` server. The in-process `WebSocketTestClient` tests only reach
`src/testing.rs::handle_test_websocket`, so this is the only coverage for the
production builder rejecting oversized path/query params with an HTTP 400 instead
of passing the raw string through to Python.
"""

from __future__ import annotations

import base64
import secrets
import socket

import pytest

from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def _attempt_ws_upgrade(host: str, port: int, path: str, *, timeout: float = 5.0) -> tuple[str, str]:
    """Send a raw WebSocket upgrade request and return (status_line, body_text).

    Unlike `SimpleWebSocketClient`, this captures the full response (including
    the error body) so a *rejected* upgrade can be inspected rather than
    asserting a 101 handshake.
    """
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = "\r\n".join(
        [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "",
            "",
        ]
    )

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request.encode("utf-8"))

        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)

        header_part, _, body_part = bytes(buf).partition(b"\r\n\r\n")
        header_text = header_part.decode("utf-8", errors="replace")

        content_length = 0
        for line in header_text.splitlines()[1:]:
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
                break

        body = bytearray(body_part)
        while len(body) < content_length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body.extend(chunk)

    status_line = header_text.splitlines()[0] if header_text else ""
    return status_line, body.decode("utf-8", errors="replace")


def _make_ws_project(make_server_project):
    return make_server_project(
        project_api_body="""
        @api.websocket("/ws/path/{value}")
        async def path_ws(websocket: WebSocket, value: str):
            await websocket.accept()
            await websocket.send_text("connected")

        @api.websocket("/ws/plain")
        async def plain_ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("connected")
        """
    )


def test_websocket_oversized_path_param_rejects_upgrade(make_server_project):
    """An oversized path param rejects the upgrade with 400 instead of upgrading."""
    project = _make_ws_project(make_server_project)
    oversized = "a" * 9000  # exceeds the default 8192-byte limit

    with project.start() as server:
        status_line, body = _attempt_ws_upgrade(server.host, server.port, f"/ws/path/{oversized}")

    assert "400" in status_line, f"Expected 400 rejection, got status={status_line!r} body={body!r}"
    assert "Parameter too long" in body, f"body={body!r}"


def test_websocket_oversized_query_param_rejects_upgrade(make_server_project):
    """An oversized query param rejects the upgrade with 400 instead of upgrading."""
    project = _make_ws_project(make_server_project)
    oversized = "a" * 9000

    with project.start() as server:
        status_line, body = _attempt_ws_upgrade(server.host, server.port, f"/ws/plain?value={oversized}")

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
        status_line, body = _attempt_ws_upgrade(server.host, server.port, f"/ws/path/{rejected}")
        assert "400" in status_line, f"Expected 400 rejection, got status={status_line!r} body={body!r}"
        assert "Parameter too long" in body, f"body={body!r}"

        with SimpleWebSocketClient(server.host, server.port, f"/ws/path/{accepted}") as websocket:
            assert websocket.receive_text() == "connected"

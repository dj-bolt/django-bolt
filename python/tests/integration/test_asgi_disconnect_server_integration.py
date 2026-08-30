from __future__ import annotations

import socket

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def _abort_request(server, path: str, *, after_headers: bool) -> None:
    sock = socket.create_connection((server.host, server.port), timeout=5)
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {server.host}\r\n\r\n".encode())
    if after_headers:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            assert chunk, "server closed before sending headers"
            head += chunk
        assert b"200 OK" in head
    sock.close()


def test_client_abort_before_headers_delivers_http_disconnect(make_server_project):
    project = make_server_project(api_module=app_module("asgi_disconnect"))

    with project.start() as server:
        _abort_request(server, "/mounted/wait", after_headers=False)
        events = server.wait_for_json("/events", lambda data: bool(data["events"]), timeout=2.5)

    assert events["events"] == ["http.disconnect"]


def test_client_abort_mid_body_delivers_http_disconnect_and_send_is_silent(make_server_project):
    project = make_server_project(api_module=app_module("asgi_disconnect"))

    with project.start() as server:
        _abort_request(server, "/mounted/stream", after_headers=True)
        events = server.wait_for_json("/events", lambda data: len(data["events"]) >= 2, timeout=2.5)

    assert events["events"] == ["http.disconnect", "late-send-ok"]

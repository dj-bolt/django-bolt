"""A 204 response never writes a body, so a keep-alive connection stays in sync."""

from __future__ import annotations

import socket

import pytest

from .apps import app_module


def _pipeline(server, path: str) -> bytes:
    """Send a 204 request and a GET /health on one connection; return the raw reply."""
    with socket.create_connection((server.host, server.port), timeout=5) as sock:
        sock.sendall(
            f"DELETE {path} HTTP/1.1\r\nHost: {server.host}\r\n\r\n"
            f"GET /health HTTP/1.1\r\nHost: {server.host}\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.server_integration
@pytest.mark.parametrize("path", ["/response", "/json", "/django", "/none"])
def test_204_has_no_body_on_a_reused_connection(make_server_project, path):
    project = make_server_project(api_module=app_module("no_content"))
    with project.start(startup_path="/health") as server:
        raw = _pipeline(server, path)

    first, _, rest = raw.partition(b"\r\n\r\n")
    assert first.startswith(b"HTTP/1.1 204 No Content")
    assert b"content-length" not in first.lower()
    assert rest.startswith(b"HTTP/1.1 200 OK"), rest[:80]
    assert rest.endswith(b'{"status":"ok"}')

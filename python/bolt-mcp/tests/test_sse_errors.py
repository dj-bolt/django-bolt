"""GET /mcp error paths that return immediately (no long-lived stream).

The live SSE listen channel, 409-on-second-stream, and DELETE-closes-stream are
covered by the subprocess integration tests — the buffered TestClient cannot
hold an open stream.
"""

from __future__ import annotations

from _helpers import make_server

from django_bolt.testing import TestClient


def test_get_on_stateless_server_returns_405():
    api, _ = make_server(stateless=True)
    with TestClient(api) as client:
        resp = client.get("/mcp", headers={"Accept": "text/event-stream"})
        assert resp.status_code == 405
        assert "POST" in resp.headers.get("allow", "")


def test_get_without_event_stream_accept_returns_406():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = client.get("/mcp", headers={"Accept": "application/json"})
        assert resp.status_code == 406


def test_get_with_unknown_session_returns_404():
    api, _ = make_server()
    with TestClient(api) as client:
        resp = client.get(
            "/mcp",
            headers={"Accept": "text/event-stream", "Mcp-Session-Id": "bogus"},
        )
        assert resp.status_code == 404

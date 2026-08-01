"""Real-server integration for micro-reflex: page GET + WS event round-trip.

Exercises the full production path — runbolt startup, Actix HTTP serving the
pre-rendered page, and a real TCP WebSocket handshake driving an event through
dispatch → state mutation → slot diff → patch frame.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_microreflex_page_and_event_roundtrip(make_server_project):
    project = make_server_project(api_module=app_module("microreflex_demo"))

    with project.start() as server:
        response = server.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "micro-reflex on django-bolt" in response.text
        assert "data-rx-on=" in response.text

        with SimpleWebSocketClient(server.host, server.port, "/_rx/ws?page=/") as websocket:
            websocket.send_text(json.dumps({"t": "e", "h": {"s": "DemoCounterState", "n": "increment"}}))
            message = json.loads(websocket.receive_text())
            assert message["t"] == "p"
            text_values = {p["v"] for p in message["p"] if p["k"] == "t"}
            assert "1" in text_values
            assert "odd" in text_values


def test_browser_same_origin_websocket_handshake_allowed(make_server_project):
    """Browsers ALWAYS send Origin on WebSocket handshakes — a same-origin
    Origin (matching the Host header) must be accepted without CORS config."""
    project = make_server_project(api_module=app_module("microreflex_demo"))

    with project.start() as server:
        origin = f"http://{server.host}:{server.port}"
        with SimpleWebSocketClient(
            server.host,
            server.port,
            "/_rx/ws?page=/",
            headers={"Origin": origin},
        ) as websocket:
            websocket.send_text(json.dumps({"t": "e", "h": {"s": "DemoCounterState", "n": "increment"}}))
            message = json.loads(websocket.receive_text())
            assert message["t"] == "p"


def test_cross_origin_websocket_handshake_denied_without_cors(make_server_project):
    project = make_server_project(api_module=app_module("microreflex_demo"))

    with project.start() as server:
        client = SimpleWebSocketClient(
            server.host,
            server.port,
            "/_rx/ws?page=/",
            headers={"Origin": "https://evil.example"},
        )
        with pytest.raises(AssertionError, match="403"):
            client.connect()

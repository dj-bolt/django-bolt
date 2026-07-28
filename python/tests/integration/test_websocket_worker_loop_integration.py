"""WebSocket handlers must share the WorkerLoop with HTTP dispatch.

Asyncio primitives (futures, queues, locks) shared between a WebSocket
handler and an HTTP handler only work when both coroutines run on the same
event loop — resolving a future from a foreign loop queues a wakeup that
never fires. This test is the canary for that loop-identity invariant, the
WebSocket counterpart of the bolt-mcp sampling round-trip.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_http_handler_resolves_future_awaited_by_websocket(make_server_project):
    project = make_server_project(api_module=app_module("ws_http_bridge"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/wait") as websocket,
    ):
        assert websocket.receive_text() == "ready"

        response = server.client.post(server.url("/resolve"))
        assert response.status_code == 200
        assert response.json() == {"resolved": True}

        # Cross-loop, the wakeup is lost and the handler times out instead.
        assert websocket.receive_text() == "from-http"

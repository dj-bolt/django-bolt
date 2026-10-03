from __future__ import annotations

import pytest

from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.testing import TestClient, WebSocketTestClient

from .apps import app_module, include_in_schema_layers
from .helpers import SimpleWebSocketClient

HIDDEN = ("/route-hidden", "/view-hidden", "/internal/secret")
SHOWN = ("/public", "/internal/shown")
WS_HIDDEN = ("/ws/route-hidden", "/internal/ws/secret")
WS_SHOWN = ("/ws/default", "/ws/tagged", "/internal/ws/shown")
# Each WebSocket operation has one tag group, so renderers list it once (#368).
WS_TAGS = {"/ws/default": ["WebSocket"], "/ws/tagged": ["Streaming"], "/internal/ws/shown": ["Internal"]}


def _assert_websocket_operations(spec: dict) -> None:
    paths = set(spec["paths"])
    assert set(WS_SHOWN) <= paths
    assert not paths & set(WS_HIDDEN)
    assert {path: spec["paths"][path]["get"]["tags"] for path in WS_SHOWN} == WS_TAGS
    assert spec["paths"]["/ws/tagged"]["get"]["summary"] == "WebSocket: Stream prices"


@pytest.mark.server_integration
def test_layered_include_in_schema_over_real_server(make_server_project):
    project = make_server_project(api_module=app_module("include_in_schema_layers"))

    with project.start() as server:
        spec = server.get("/docs/openapi.json").json()
        served = {path: server.get(path).status_code for path in HIDDEN + SHOWN}
        ws_served = {}
        for path in WS_HIDDEN + WS_SHOWN:
            with SimpleWebSocketClient(server.host, server.port, path) as websocket:
                ws_served[path] = websocket.receive_text()

    paths = set(spec["paths"])
    assert set(SHOWN) <= paths
    assert not paths & set(HIDDEN)
    assert served == dict.fromkeys(HIDDEN + SHOWN, 200)
    _assert_websocket_operations(spec)
    assert ws_served == {path: path for path in WS_HIDDEN + WS_SHOWN}


def test_layered_include_in_schema_in_process():
    """The `/docs` routes only exist under `runbolt`; read the schema directly here."""
    api = include_in_schema_layers.api
    spec = SchemaGenerator(api, OpenAPIConfig(title="t", version="1")).generate().to_schema()
    paths = set(spec["paths"])
    with TestClient(api) as client:
        served = {path: client.get(path).status_code for path in HIDDEN + SHOWN}

    assert set(SHOWN) <= paths
    assert not paths & set(HIDDEN)
    assert served == dict.fromkeys(HIDDEN + SHOWN, 200)
    _assert_websocket_operations(spec)


@pytest.mark.asyncio
async def test_layered_include_in_schema_websocket_routes_served_in_process():
    """A WebSocket route out of the schema still accepts connections."""
    for path in WS_HIDDEN + WS_SHOWN:
        async with WebSocketTestClient(include_in_schema_layers.api, path) as websocket:
            assert await websocket.receive_text() == path

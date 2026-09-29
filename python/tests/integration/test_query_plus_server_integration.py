"""Query-string decoding through the production handlers.

`TestClient` reaches `src/testing.rs` only. These tests run `runbolt` and
reach `src/handler.rs` and the production WebSocket `build_scope`.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def test_runbolt_decodes_plus_in_query_as_space(make_server_project):
    project = make_server_project(api_module=app_module("query_plus"))

    with project.start() as server:
        plus = server.get("/search?q=hello+world%2B1")
        params = server.get("/search", params={"q": "x y"})
        path = server.get("/items/a+b")

    assert plus.status_code == 200, plus.text
    assert plus.json() == {"q": "hello world+1"}
    assert params.status_code == 200, params.text
    assert params.json() == {"q": "x y"}
    assert path.status_code == 200, path.text
    assert path.json() == {"name": "a+b"}


def test_runbolt_query_matches_django_and_urls_keep_the_query(make_server_project):
    project = make_server_project(api_module=app_module("query_plus"))
    query = "q=a+%FF&tag=a%2Bb&text=a%26b%3Dc&=v&flag"

    with project.start() as server:
        response = server.get(f"/echo?{query}")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "query": {"q": "a \ufffd", "tag": "a+b", "text": "a&b=c", "": "v", "flag": ""},
        "full_path": f"/echo?{query}",
        "query_string": query,
    }


def test_runbolt_websocket_decodes_query_as_django(make_server_project):
    project = make_server_project(api_module=app_module("query_plus"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/search?q=hello+world%2B1%FF&flag") as websocket,
    ):
        message = json.loads(websocket.receive_text())

    assert message == {"q": "hello world+1\ufffd", "flag": ""}

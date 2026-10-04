"""A handler that hands on the request gets the body on a real server.

Bolt reads the source of a handler to find the request parts that it uses.
The server skips the other parts. When a handler hands on the request, code
that Bolt does not read can use any part. Thus the handler must get every
part. Before the fix, each route here got an empty body.

This test needs runbolt. The request pipeline of TestClient reads the body of
each request, so TestClient does not show the bug.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

HAND_ON_PATHS = ("/positional", "/keyword", "/sync", "/alias", "/container", "/attribute", "/path/1")


def test_a_request_handed_on_keeps_its_body(make_server_project):
    project = make_server_project(api_module=app_module("request_hand_on"))

    with project.start() as server:
        responses = {path: server.request("POST", path, content=b"payload") for path in HAND_ON_PATHS}
        items = server.request("POST", "/items", content=b"payload")

    assert {path: (r.status_code, r.json()) for path, r in responses.items()} == {
        path: (200, {"body": "payload", "user": 1}) for path in HAND_ON_PATHS
    }
    assert (items.status_code, items.json()) == (201, {"body": "payload"})

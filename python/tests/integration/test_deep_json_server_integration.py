"""Deeply nested JSON on a real ``runbolt`` server.

msgspec uses C stack for each level of nesting. On Python 3.12 and 3.13, the
C recursion limit allows more levels than a 2 MiB thread stack holds. Actix
starts its worker threads with that stack. A deep body thus stopped the server
with SIGSEGV. Only a real server runs requests on Actix worker threads.
"""

from __future__ import annotations

import pytest

from .apps import app_module

# This depth fits the stack of the main thread. It overflowed a 2 MiB stack.
DEEP = 7_000
# No thread stack holds this depth. The decoder raises RecursionError.
TOO_DEEP = 100_000


def nested(depth: int) -> bytes:
    return b'{"value":' + b"[" * depth + b"]" * depth + b"}"


def post(server, path: str, depth: int):
    return server.request("POST", path, content=nested(depth), headers={"content-type": "application/json"})


@pytest.fixture
def deep_json_server(make_server_project):
    module = app_module("deep_json")
    # runbolt loads one API for each BOLT_API entry. The lane routes are on a second API.
    both_apis = f"\nBOLT_API = [{module!r}, {module.split(':', 1)[0] + ':lane_api'!r}]\n"
    project = make_server_project(api_module=module, settings_extra=both_apis)
    with project.start() as server:
        yield server


@pytest.mark.server_integration
def test_deep_json_body_is_decoded_on_a_worker_and_a_lane(deep_json_server):
    worker = post(deep_json_server, "/body", DEEP)
    assert worker.status_code == 200, worker.text

    lane = post(deep_json_server, "/lane/body", DEEP)
    assert lane.status_code == 200, lane.text
    assert lane.json() == {"on_lane": True}


@pytest.mark.server_integration
@pytest.mark.parametrize("path", ["/body", "/dict-body", "/lane/body"])
def test_too_deep_json_body_gets_422_and_the_server_keeps_serving(deep_json_server, path):
    response = post(deep_json_server, path, TOO_DEEP)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [{"type": "json_invalid", "loc": ["body"], "msg": "JSON is nested too deeply"}]
    assert deep_json_server.get("/health").status_code == 200


@pytest.mark.server_integration
def test_handler_that_decodes_deep_json_gets_500_and_the_server_keeps_serving(deep_json_server):
    assert post(deep_json_server, "/decode", DEEP).status_code == 200

    # The RecursionError of the handler is a server error, as any other exception of a handler.
    assert post(deep_json_server, "/decode", TOO_DEEP).status_code == 500
    assert deep_json_server.get("/health").status_code == 200

"""Two-altitude coverage for the eager loop-thread async dispatch bridge.

The production async path (handler.rs → _bridge.eager_dispatch) only runs in a
real ``runbolt`` server — TestClient bridges through run_coroutine_threadsafe.
The subprocess test drives every async-bridge shape (never-suspends, bare
yield, real Future, exceptions, dependency gather, create_task, streaming)
through the eager bridge, and again with ``DJANGO_BOLT_EAGER_DISPATCH=0`` to
pin that the legacy Task-per-request bridge produces identical responses.
"""

from __future__ import annotations

import pytest

from django_bolt.testing import TestClient

from .apps import app_module
from .apps import dispatch_probes as probes_app

PAYLOAD = {"message": "hello", "n": 1}

EXPECTATIONS = [
    ("/t-sync", 200, PAYLOAD),
    ("/t-trivial", 200, PAYLOAD),
    ("/t-ready", 200, PAYLOAD),
    ("/t-sleep0", 200, PAYLOAD),
    ("/t-thread", 200, {"value": "from-thread"}),
    ("/t-exc", 418, {"detail": "teapot-after-await"}),
    ("/t-deps", 200, {"a": "a", "b": "b"}),
    ("/t-task", 200, {"loop_running": True, "task_result": "a"}),
]


def _assert_probes(client) -> None:
    for path, status, payload in EXPECTATIONS:
        response = client.get(path)
        assert response.status_code == status, path
        assert response.json() == payload, path

    stream = client.get("/t-stream")
    assert stream.status_code == 200
    assert stream.text == '{"i": 0}\n{"i": 1}\n{"i": 2}\n'


def test_dispatch_probes_in_process():
    with TestClient(probes_app.api) as client:
        _assert_probes(client)


@pytest.mark.server_integration
def test_dispatch_probes_eager_bridge(make_server_project):
    project = make_server_project(api_module=app_module("dispatch_probes"))
    with project.start() as server:
        _assert_probes(server)


@pytest.mark.server_integration
def test_dispatch_probes_legacy_bridge(make_server_project):
    """DJANGO_BOLT_EAGER_DISPATCH=0 must restore the Task-per-request bridge
    with identical observable behavior."""
    project = make_server_project(api_module=app_module("dispatch_probes"))
    with project.start(env={"DJANGO_BOLT_EAGER_DISPATCH": "0"}) as server:
        _assert_probes(server)

"""MCP Tasks (SEP-2663) over a real runbolt server and real TCP.

The task outlives the ``tools/call`` request, so each ``tasks/get`` poll is a
new HTTP request that must reach the running task on the server.
"""

from __future__ import annotations

import time

import pytest
from _helpers import mcp_app_source, parse_rpc, post_rpc_modern

pytestmark = pytest.mark.server_integration

TASK_CAPS = {"extensions": {"io.modelcontextprotocol/tasks": {}}, "elicitation": {}}


class _ServerClient:
    """Adapt a running server to the ``client.post("/mcp", ...)`` shape of ``post_rpc_modern``."""

    def __init__(self, server):
        self._server = server

    def post(self, path, **kwargs):
        return self._server.client.post(self._server.url(path), **kwargs)


def _rpc(client, method, params, request_id=1):
    resp = post_rpc_modern(client, method, params, id=request_id, capabilities=TASK_CAPS)
    assert resp.status_code == 200, resp.text
    return parse_rpc(resp)["result"]


def _poll(client, task_id, until, timeout=10.0):
    deadline = time.monotonic() + timeout
    statuses = []
    while time.monotonic() < deadline:
        result = _rpc(client, "tasks/get", {"taskId": task_id})
        statuses.append(result["status"])
        if until(result):
            return result, statuses
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach the expected state; statuses: {statuses}")


def test_task_create_poll_complete_over_tcp(make_server_project):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    with project.start() as server:
        client = _ServerClient(server)
        created = _rpc(client, "tools/call", {"name": "multiply", "arguments": {"a": 6, "b": 7}})
        assert created["resultType"] == "task"
        assert created["status"] == "working"
        assert created["pollIntervalMs"] == 20

        final, statuses = _poll(client, created["taskId"], lambda r: r["status"] == "completed")
        # The tool sleeps, so at least one poll saw the task still running.
        assert "working" in statuses
        assert final["result"]["structuredContent"] == {"product": 42, "task_id": created["taskId"]}
        assert final["statusMessage"] == "multiplying"


def test_in_task_elicitation_over_tcp(make_server_project):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    with project.start() as server:
        client = _ServerClient(server)
        created = _rpc(client, "tools/call", {"name": "confirm", "arguments": {}})
        task_id = created["taskId"]
        waiting, _ = _poll(client, task_id, lambda r: r["status"] == "input_required")
        ((key, request),) = waiting["inputRequests"].items()
        assert request["method"] == "elicitation/create"

        _rpc(
            client,
            "tasks/update",
            {"taskId": task_id, "inputResponses": {key: {"action": "accept", "content": {"go": True}}}},
        )
        final, _ = _poll(client, task_id, lambda r: r["status"] == "completed")
        assert final["result"]["structuredContent"] == {"answer": {"go": True}}


@pytest.mark.parametrize(
    ("spawn_kwargs", "option"),
    [
        ({"processes": 2}, "--processes above 1"),
        # Recycling replaces a worker spawn-first: tasks/get can reach the
        # new worker while the task still runs in the old one.
        ({"extra_args": ["--workers-lifetime", "3600"]}, "--workers-lifetime"),
        ({"extra_args": ["--max-rss", "4096"]}, "--max-rss"),
    ],
)
def test_task_tools_reject_options_that_split_tasks_across_processes(make_server_project, spawn_kwargs, option):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    process, _port = project.spawn(**spawn_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=30)
    finally:
        process.kill()
    assert process.returncode != 0, stdout
    assert option in stderr
    assert "confirm, multiply" in stderr

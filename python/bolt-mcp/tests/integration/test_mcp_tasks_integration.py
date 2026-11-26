"""MCP tasks (SEP-2663) over a real runbolt server and real TCP.

The task outlives its ``tools/call`` request. Each ``tasks/get`` here opens a
new connection, so the polls reach different worker threads of the process.
Tasks live in the memory of one process, so ``runbolt`` refuses the options
that let another process get a ``tasks/get``.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from _helpers import mcp_app_source, parse_rpc, post_rpc_modern

pytestmark = pytest.mark.server_integration

TASK_CAPS = {"extensions": {"io.modelcontextprotocol/tasks": {}}, "elicitation": {}}


class _NewConnectionClient:
    """Send each request of ``post_rpc_modern`` on a new TCP connection."""

    def __init__(self, server):
        self._server = server

    def post(self, path, **kwargs):
        with httpx.Client(timeout=10) as client:
            return client.post(self._server.url(path), **kwargs)


def _rpc(client, method, params):
    resp = post_rpc_modern(client, method, params, capabilities=TASK_CAPS)
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


def test_a_task_runs_on_after_its_request_and_polls_reach_it(make_server_project):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    with project.start(extra_args=["--workers", "2"]) as server:
        client = _NewConnectionClient(server)
        created = _rpc(client, "tools/call", {"name": "multiply", "arguments": {"a": 6, "b": 7}})
        assert created["resultType"] == "task"
        assert created["status"] == "working"
        assert created["pollIntervalMs"] == 20

        final, _ = _poll(client, created["taskId"], lambda r: r["status"] == "completed")
        assert final["result"]["structuredContent"] == {"product": 42, "task_id": created["taskId"]}
        assert final["statusMessage"] == "multiplying"


def test_elicit_in_a_task_over_tcp(make_server_project):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    with project.start() as server:
        client = _NewConnectionClient(server)
        task_id = _rpc(client, "tools/call", {"name": "confirm", "arguments": {}})["taskId"]
        waiting, _ = _poll(client, task_id, lambda r: r["status"] == "input_required")
        ((key, request),) = waiting["inputRequests"].items()
        assert request["method"] == "elicitation/create"

        answer = {"action": "accept", "content": {"go": True}}
        _rpc(client, "tasks/update", {"taskId": task_id, "inputResponses": {key: answer}})
        final, _ = _poll(client, task_id, lambda r: r["status"] == "completed")
        assert final["result"]["structuredContent"] == {"answer": {"go": True}}


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} did not appear")
        time.sleep(0.02)


def test_sigterm_while_a_sync_task_runs_logs_no_error(make_server_project, tmp_path):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    with project.start() as server:
        client = _NewConnectionClient(server)
        arguments = {"seconds": 1.5, "marker": str(tmp_path)}
        _rpc(client, "tools/call", {"name": "sync_sleep", "arguments": arguments})
        _wait_for(tmp_path / "started")
        stdout, stderr = server.stop()
    assert server.process.returncode == 0, stdout + stderr
    output = stdout + stderr
    assert "Traceback" not in output, output
    assert "ERROR" not in output, output


def test_sigterm_cancels_a_running_async_task_and_its_cleanup_runs(make_server_project, tmp_path):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    with project.start() as server:
        client = _NewConnectionClient(server)
        _rpc(client, "tools/call", {"name": "cleanup", "arguments": {"marker": str(tmp_path)}})
        _wait_for(tmp_path / "started")
        stdout, stderr = server.stop()
    assert (tmp_path / "finally").exists(), "the finally block of the task did not run at shutdown"
    output = stdout + stderr
    assert "Traceback" not in output, output


@pytest.mark.parametrize(
    ("spawn_kwargs", "option"),
    [
        ({"processes": 2}, "--processes 2"),
        # Recycling starts the new worker before the old one stops: a
        # tasks/get can reach the new worker while the task runs in the old one.
        ({"extra_args": ["--workers-lifetime", "3600"]}, "--workers-lifetime"),
        ({"extra_args": ["--max-rss", "4096"]}, "--max-rss"),
    ],
)
def test_runbolt_refuses_task_tools_with_more_than_one_process(make_server_project, spawn_kwargs, option):
    project = make_server_project(api_source=mcp_app_source("tasks"))
    process, _port = project.spawn(**spawn_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=60)
    finally:
        process.kill()
    assert process.returncode != 0, stdout
    assert option in stderr
    assert "task tools (cleanup, confirm, multiply, sync_sleep)" in stderr

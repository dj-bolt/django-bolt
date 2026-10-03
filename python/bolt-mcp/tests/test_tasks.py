"""MCP Tasks extension (SEP-2663): ``@mcp.tool(task=True)`` returns ``resultType: "task"``."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from _helpers import mint_jwt, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI, JWTAuthentication
from django_bolt.testing import TestClient

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
TASK_CAPS = {"extensions": {TASKS_EXTENSION: {}}}
TASK_ELICIT_CAPS = {"extensions": {TASKS_EXTENSION: {}}, "elicitation": {}}
MISSING_CLIENT_CAPABILITY = -32021
INVALID_PARAMS = -32602
SECRET = "tasks-secret-key-0123456789-abcdefghij"


def _build(*, auth=None):
    api = BoltAPI()
    mcp = MCP("task-server")
    events: dict[str, threading.Event] = {
        "slow_cancelled": threading.Event(),
        "expiring_cancelled": threading.Event(),
    }
    runs = {"ask": 0}

    @mcp.tool(task=True, poll_interval=25)
    async def add(a: int, b: int) -> dict:
        await asyncio.sleep(0.05)
        return {"sum": a + b}

    @mcp.tool(task=True)
    async def explode() -> dict:
        raise ValueError("task kaboom")

    @mcp.tool(task=True)
    async def slow() -> dict:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events["slow_cancelled"].set()
            raise
        return {"done": True}

    @mcp.tool(task=True, ttl=100)
    async def expiring() -> dict:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events["expiring_cancelled"].set()
            raise
        return {"done": True}

    @mcp.tool(task=True)
    async def ask(ctx: Context) -> dict:
        runs["ask"] += 1
        answer = await ctx.elicit("Proceed?")
        return {"answer": answer, "runs": runs["ask"], "task_id": ctx.task_id}

    @mcp.tool(task=True)
    async def progress(ctx: Context) -> dict:
        await ctx.report_progress(1, 4, "step one of four")
        await asyncio.sleep(30)
        return {}

    @mcp.tool
    async def plain(ctx: Context) -> dict:
        return {"task_id": ctx.task_id}

    mount_mcp(api, mcp, auth=auth)
    return api, events, runs


def _call(client, name, arguments=None, *, capabilities=TASK_CAPS, authorization=None, id=1):
    resp = post_rpc_modern(
        client,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        id=id,
        capabilities=capabilities,
        authorization=authorization,
    )
    assert resp.status_code == 200, resp.content
    return parse_rpc(resp)


def _task_rpc(client, method, params, *, capabilities=TASK_CAPS, authorization=None):
    resp = post_rpc_modern(client, method, params, capabilities=capabilities, authorization=authorization)
    assert resp.status_code == 200, resp.content
    return parse_rpc(resp)


def _rpc_error(client, method, params, *, capabilities=TASK_CAPS, authorization=None):
    """A JSON-RPC error answer; rmcp sends request errors with HTTP 400."""
    resp = post_rpc_modern(client, method, params, capabilities=capabilities, authorization=authorization)
    assert resp.status_code == 400, resp.content
    return parse_rpc(resp)["error"]


def _get(client, task_id, **kwargs):
    return _task_rpc(client, "tasks/get", {"taskId": task_id}, **kwargs)


def _poll(client, task_id, *, until, timeout=5.0, **kwargs):
    deadline = time.monotonic() + timeout
    while True:
        result = _get(client, task_id, **kwargs)["result"]
        if until(result):
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"task {task_id} never reached the expected state; last: {result}")
        time.sleep(0.02)


def _terminal(result):
    return result["status"] in {"completed", "failed", "cancelled"}


def _create(client, name, arguments=None, **kwargs):
    result = _call(client, name, arguments, **kwargs)["result"]
    assert result["resultType"] == "task", result
    return result


def test_capability_advertised_only_when_a_task_tool_exists():
    api, _, _ = _build()
    with TestClient(api) as client:
        caps = _task_rpc(client, "server/discover", {})["result"]["capabilities"]
        assert TASKS_EXTENSION in caps["extensions"]

    plain_api = BoltAPI()
    plain_mcp = MCP("no-tasks")

    @plain_mcp.tool
    async def noop() -> dict:
        return {}

    mount_mcp(plain_api, plain_mcp)
    with TestClient(plain_api) as client:
        caps = _task_rpc(client, "server/discover", {})["result"]["capabilities"]
        assert TASKS_EXTENSION not in (caps.get("extensions") or {})


def test_create_poll_complete():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "add", {"a": 40, "b": 2})
        assert created["status"] == "working"
        assert created["taskId"]
        assert created["pollIntervalMs"] == 25
        assert created["ttlMs"] == 300_000

        final = _poll(client, created["taskId"], until=_terminal)
        assert final["status"] == "completed"
        assert final["resultType"] == "complete"
        assert final["result"]["structuredContent"] == {"sum": 42}
        assert final["result"]["isError"] is False


def test_tool_exception_completes_with_in_band_error():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "explode")
        final = _poll(client, created["taskId"], until=_terminal)
        assert final["status"] == "completed"
        assert final["result"]["isError"] is True
        assert "task kaboom" in final["result"]["content"][0]["text"]


def test_invalid_arguments_complete_with_in_band_error():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "add", {"a": "nope", "b": 2})
        final = _poll(client, created["taskId"], until=_terminal)
        assert final["result"]["isError"] is True
        assert "Invalid arguments" in final["result"]["content"][0]["text"]


def test_cancel_stops_the_python_coroutine():
    api, events, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "slow")
        ack = _task_rpc(client, "tasks/cancel", {"taskId": created["taskId"]})
        assert "error" not in ack, ack
        final = _poll(client, created["taskId"], until=_terminal)
        assert final["status"] == "cancelled"
        assert events["slow_cancelled"].wait(5), "tasks/cancel did not cancel the tool coroutine"


def test_ttl_expiry_fails_the_task_and_stops_the_coroutine():
    api, events, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "expiring")
        assert created["ttlMs"] == 100
        time.sleep(0.15)
        final = _get(client, created["taskId"])["result"]
        assert final["status"] == "failed"
        assert "TTL" in final["error"]["message"]
        assert events["expiring_cancelled"].wait(5), "TTL expiry did not cancel the tool coroutine"


def test_tool_without_task_caps_runs_synchronously():
    api, _, _ = _build()
    with TestClient(api) as client:
        result = _call(client, "add", {"a": 1, "b": 2}, capabilities={})["result"]
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == {"sum": 3}


def test_tasks_methods_without_task_caps_are_missing_capability():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "add", {"a": 1, "b": 1})
        for method, params in (
            ("tasks/get", {"taskId": created["taskId"]}),
            ("tasks/update", {"taskId": created["taskId"], "inputResponses": {}}),
            ("tasks/cancel", {"taskId": created["taskId"]}),
        ):
            error = _rpc_error(client, method, params, capabilities={})
            assert error["code"] == MISSING_CLIENT_CAPABILITY, method
            assert TASKS_EXTENSION in error["data"]["requiredCapabilities"]["extensions"]


def test_unknown_task_is_invalid_params():
    api, _, _ = _build()
    with TestClient(api) as client:
        error = _rpc_error(client, "tasks/get", {"taskId": "no-such-task"})
        assert error["code"] == INVALID_PARAMS


def test_non_task_tool_stays_synchronous_for_task_clients():
    api, _, _ = _build()
    with TestClient(api) as client:
        result = _call(client, "plain")["result"]
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == {"task_id": None}


def test_in_task_elicitation_resumes_through_tasks_update():
    api, _, runs = _build()
    with TestClient(api) as client:
        created = _create(client, "ask", capabilities=TASK_ELICIT_CAPS)
        task_id = created["taskId"]
        waiting = _poll(
            client,
            task_id,
            until=lambda r: r["status"] == "input_required",
            capabilities=TASK_ELICIT_CAPS,
        )
        ((key, request),) = waiting["inputRequests"].items()
        assert request["method"] == "elicitation/create"
        assert request["params"]["message"] == "Proceed?"

        ack = _task_rpc(
            client,
            "tasks/update",
            {"taskId": task_id, "inputResponses": {key: {"action": "accept", "content": {"ok": True}}}},
            capabilities=TASK_ELICIT_CAPS,
        )
        assert "error" not in ack, ack

        final = _poll(client, task_id, until=_terminal, capabilities=TASK_ELICIT_CAPS)
        assert final["status"] == "completed"
        sc = final["result"]["structuredContent"]
        assert sc["answer"] == {"action": "accept", "content": {"ok": True}}
        # The coroutine suspended and resumed: no MRTR replay from the top.
        assert sc["runs"] == 1
        assert sc["task_id"] == task_id


def test_in_task_elicitation_without_client_capability_is_in_band_error():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "ask")
        final = _poll(client, created["taskId"], until=_terminal)
        assert final["status"] == "completed"
        assert final["result"]["isError"] is True
        assert "elicitation" in final["result"]["content"][0]["text"]


def test_cancel_wakes_a_task_waiting_for_input():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "ask", capabilities=TASK_ELICIT_CAPS)
        task_id = created["taskId"]
        _poll(client, task_id, until=lambda r: r["status"] == "input_required", capabilities=TASK_ELICIT_CAPS)
        _task_rpc(client, "tasks/cancel", {"taskId": task_id}, capabilities=TASK_ELICIT_CAPS)
        final = _poll(client, task_id, until=_terminal, capabilities=TASK_ELICIT_CAPS)
        assert final["status"] == "cancelled"


def test_progress_sets_status_message():
    api, _, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "progress")
        result = _poll(client, created["taskId"], until=lambda r: r.get("statusMessage") is not None)
        assert result["status"] == "working"
        assert result["statusMessage"] == "step one of four"
        _task_rpc(client, "tasks/cancel", {"taskId": created["taskId"]})


def test_task_is_bound_to_the_creating_principal():
    api, _, _ = _build(auth=[JWTAuthentication(secret=SECRET)])
    alice = f"Bearer {mint_jwt(SECRET, sub='alice')}"
    mallory = f"Bearer {mint_jwt(SECRET, sub='mallory')}"
    with TestClient(api) as client:
        created = _create(client, "slow", authorization=alice)
        task_id = created["taskId"]
        for method, params in (
            ("tasks/get", {"taskId": task_id}),
            ("tasks/update", {"taskId": task_id, "inputResponses": {}}),
            ("tasks/cancel", {"taskId": task_id}),
        ):
            error = _rpc_error(client, method, params, authorization=mallory)
            assert error["code"] == INVALID_PARAMS, method
        assert _get(client, task_id, authorization=alice)["result"]["status"] == "working"
        _task_rpc(client, "tasks/cancel", {"taskId": task_id}, authorization=alice)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ttl": 1000}, "task=True"),
        ({"poll_interval": 10}, "task=True"),
        ({"task": True, "ttl": 0}, "ttl"),
        ({"task": True, "poll_interval": -1}, "poll_interval"),
    ],
)
def test_task_options_are_validated(kwargs, message):
    mcp = MCP("bad")
    with pytest.raises(ValueError, match=message):

        @mcp.tool(**kwargs)
        async def t() -> dict:
            return {}

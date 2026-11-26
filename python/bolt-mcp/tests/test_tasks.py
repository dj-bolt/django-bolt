"""MCP Tasks extension (SEP-2663): ``@mcp.tool(task=True)`` answers with ``resultType: "task"``."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from _helpers import INITIALIZE_PARAMS, mint_jwt, parse_rpc, post_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, ToolError, mount_mcp

from django_bolt import BoltAPI, JWTAuthentication
from django_bolt.testing import TestClient

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
TASK_CAPS = {"extensions": {TASKS_EXTENSION: {}}}
TASK_ELICIT_CAPS = {"extensions": {TASKS_EXTENSION: {}}, "elicitation": {}}
MISSING_CLIENT_CAPABILITY = -32021
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SECRET = "tasks-secret-key-0123456789-abcdefghij"
TERMINAL = frozenset({"completed", "failed", "cancelled"})


class _Escape(BaseException):
    """Leaves the tool past the error handling of bolt-mcp, so the dispatch itself fails."""


class Probe:
    """What the tools of the test server saw, read from the test thread."""

    def __init__(self) -> None:
        self.cancelled = {name: threading.Event() for name in ("slow", "expiring", "progress", "ask")}
        self.ask_runs = 0
        self.sync_threads: list[str] = []


def _build(*, auth=None, max_tasks: int | None = None) -> tuple[BoltAPI, Probe]:
    api = BoltAPI()
    mcp = MCP("task-server") if max_tasks is None else MCP("task-server", max_tasks=max_tasks)
    probe = Probe()

    async def wait_for_cancel(name: str) -> dict:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            probe.cancelled[name].set()
            raise
        return {"done": True}

    @mcp.tool(task=True, poll_interval_ms=25)
    async def add(a: int, b: int) -> dict:
        await asyncio.sleep(0.05)
        return {"sum": a + b}

    @mcp.tool(task=True)
    def add_sync(a: int, b: int) -> dict:
        probe.sync_threads.append(threading.current_thread().name)
        return {"sum": a + b}

    @mcp.tool(task=True)
    async def refuse() -> dict:
        raise ToolError("task kaboom")

    @mcp.tool(task=True)
    async def escape() -> dict:
        raise _Escape("no result")

    @mcp.tool(task=True)
    async def slow() -> dict:
        return await wait_for_cancel("slow")

    @mcp.tool(task=True, ttl_ms=300)
    async def expiring() -> dict:
        return await wait_for_cancel("expiring")

    @mcp.tool(task=True, ttl_ms=200)
    async def quick() -> dict:
        return {"ok": True}

    @mcp.tool(task=True)
    async def ask(ctx: Context) -> dict:
        probe.ask_runs += 1
        try:
            answer = await ctx.elicit("Proceed?")
        except asyncio.CancelledError:
            probe.cancelled["ask"].set()
            raise
        return {"answer": answer, "runs": probe.ask_runs, "task_id": ctx.task_id}

    @mcp.tool(task=True)
    async def progress(ctx: Context) -> dict:
        await ctx.report_progress(1, 4, "step one of four")
        return await wait_for_cancel("progress")

    @mcp.tool
    async def plain(ctx: Context) -> dict:
        return {"task_id": ctx.task_id}

    mount_mcp(api, mcp, auth=auth)
    return api, probe


def _rpc(client, method, params, *, capabilities=TASK_CAPS, authorization=None):
    resp = post_rpc_modern(client, method, params, capabilities=capabilities, authorization=authorization)
    assert resp.status_code == 200, resp.content
    return parse_rpc(resp)


def _error(client, method, params, *, capabilities=TASK_CAPS, authorization=None):
    """A JSON-RPC error answer. The HTTP status depends on the error (200, 400 or 404)."""
    resp = post_rpc_modern(client, method, params, capabilities=capabilities, authorization=authorization)
    return parse_rpc(resp)["error"]


def _call(client, name, arguments=None, **kwargs):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)["result"]


def _create(client, name, arguments=None, **kwargs):
    result = _call(client, name, arguments, **kwargs)
    assert result["resultType"] == "task", result
    return result


def _get(client, task_id, **kwargs):
    return _rpc(client, "tasks/get", {"taskId": task_id}, **kwargs)["result"]


def _poll(client, task_id, *, until=lambda r: r["status"] in TERMINAL, timeout=10.0, **kwargs):
    deadline = time.monotonic() + timeout
    while True:
        result = _get(client, task_id, **kwargs)
        if until(result):
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"task {task_id} did not reach the expected state; last: {result}")
        time.sleep(0.02)


def test_the_server_advertises_tasks_only_with_a_task_tool():
    api, _ = _build()
    with TestClient(api) as client:
        caps = _rpc(client, "server/discover", {})["result"]["capabilities"]
        assert TASKS_EXTENSION in caps["extensions"]

    plain_api = BoltAPI()
    plain_mcp = MCP("no-tasks")

    @plain_mcp.tool
    async def noop() -> dict:
        return {}

    mount_mcp(plain_api, plain_mcp)
    with TestClient(plain_api) as client:
        caps = _rpc(client, "server/discover", {})["result"]["capabilities"]
        assert TASKS_EXTENSION not in (caps.get("extensions") or {})
        error = _error(client, "tasks/get", {"taskId": "any"})
        assert error["code"] == -32601


def test_create_poll_and_read_the_result():
    api, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "add", {"a": 40, "b": 2})
        assert created["status"] == "working"
        assert created["pollIntervalMs"] == 25
        assert created["ttlMs"] == 300_000
        # The task exists before tools/call returns.
        assert _get(client, created["taskId"])["status"] in {"working", "completed"}

        final = _poll(client, created["taskId"])
        assert final["status"] == "completed"
        assert final["resultType"] == "complete"
        assert final["result"]["structuredContent"] == {"sum": 42}
        assert final["result"]["isError"] is False


def test_a_sync_tool_runs_as_a_task_in_a_thread():
    api, probe = _build()
    with TestClient(api) as client:
        created = _create(client, "add_sync", {"a": 1, "b": 2})
        final = _poll(client, created["taskId"])
        assert final["result"]["structuredContent"] == {"sum": 3}
        assert len(probe.sync_threads) == 1


def test_a_tool_error_completes_the_task_with_is_error():
    api, _ = _build()
    with TestClient(api) as client:
        final = _poll(client, _create(client, "refuse")["taskId"])
        # SEP-2663: a tool error is a result, so the task is completed, not failed.
        assert final["status"] == "completed"
        assert final["result"]["isError"] is True
        assert final["result"]["content"][0]["text"] == "task kaboom"


def test_invalid_arguments_complete_the_task_with_is_error():
    api, _ = _build()
    with TestClient(api) as client:
        final = _poll(client, _create(client, "add", {"a": "nope", "b": 2})["taskId"])
        assert final["status"] == "completed"
        assert final["result"]["isError"] is True
        assert "Invalid arguments" in final["result"]["content"][0]["text"]


def test_a_failed_dispatch_fails_the_task_with_a_json_rpc_error():
    api, _ = _build()
    with TestClient(api) as client:
        final = _poll(client, _create(client, "escape")["taskId"])
        assert final["status"] == "failed"
        assert final["error"]["code"] == INTERNAL_ERROR
        assert final["statusMessage"] == final["error"]["message"]
        assert "result" not in final


def test_cancel_while_running_cancels_the_coroutine():
    api, probe = _build()
    with TestClient(api) as client:
        created = _create(client, "slow")
        ack = _rpc(client, "tasks/cancel", {"taskId": created["taskId"]})["result"]
        assert ack == {"resultType": "complete"}
        assert _get(client, created["taskId"])["status"] == "cancelled"
        assert probe.cancelled["slow"].wait(5), "tasks/cancel did not cancel the tool coroutine"


def test_cancel_after_the_task_completed_keeps_the_result():
    api, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "add", {"a": 1, "b": 1})
        _poll(client, created["taskId"])
        assert _rpc(client, "tasks/cancel", {"taskId": created["taskId"]})["result"] == {"resultType": "complete"}
        final = _get(client, created["taskId"])
        assert final["status"] == "completed"
        assert final["result"]["structuredContent"] == {"sum": 2}


def test_an_unknown_task_id_is_invalid_params():
    api, _ = _build()
    with TestClient(api) as client:
        for method, params in (
            ("tasks/get", {"taskId": "no-such-task"}),
            ("tasks/update", {"taskId": "no-such-task", "inputResponses": {}}),
            ("tasks/cancel", {"taskId": "no-such-task"}),
        ):
            error = _error(client, method, params)
            assert error["code"] == INVALID_PARAMS, method
            assert error["message"] == "Unknown task: no-such-task"


def test_the_ttl_fails_a_running_task_and_cancels_the_coroutine():
    api, probe = _build()
    with TestClient(api) as client:
        created = _create(client, "expiring")
        assert created["ttlMs"] == 300
        # No poll before the TTL: the task itself stops at its deadline.
        assert probe.cancelled["expiring"].wait(5), "the TTL did not cancel the tool coroutine"
        final = _get(client, created["taskId"])
        assert final["status"] == "failed"
        assert final["error"]["code"] == INTERNAL_ERROR
        assert "TTL" in final["error"]["message"]


def test_an_expired_task_id_is_unknown():
    api, _ = _build()
    with TestClient(api) as client:
        created = _create(client, "quick")
        assert _poll(client, created["taskId"])["status"] == "completed"
        # Bolt keeps a finished task for one TTL (200 ms), then deletes it.
        time.sleep(0.3)
        error = _error(client, "tasks/get", {"taskId": created["taskId"]})
        assert error == {"code": INVALID_PARAMS, "message": f"Unknown task: {created['taskId']}"}


def test_another_principal_cannot_reach_a_task_or_learn_that_it_exists():
    api, _ = _build(auth=[JWTAuthentication(secret=SECRET)])
    alice = f"Bearer {mint_jwt(SECRET, sub='alice')}"
    mallory = f"Bearer {mint_jwt(SECRET, sub='mallory')}"
    with TestClient(api) as client:
        created = _create(client, "slow", authorization=alice)
        task_id = created["taskId"]
        for authorization in (mallory, None):
            for method, params in (
                ("tasks/get", {"taskId": task_id}),
                ("tasks/update", {"taskId": task_id, "inputResponses": {}}),
                ("tasks/cancel", {"taskId": task_id}),
            ):
                denied = _error(client, method, params, authorization=authorization)
                unknown = _error(client, method, {**params, "taskId": "x" * len(task_id)}, authorization=authorization)
                # The same answer as for an id that does not exist.
                assert denied == {**unknown, "message": unknown["message"].replace("x" * len(task_id), task_id)}
                assert denied["code"] == INVALID_PARAMS
        assert _get(client, task_id, authorization=alice)["status"] == "working"
        _rpc(client, "tasks/cancel", {"taskId": task_id}, authorization=alice)

        # An anonymous task is out of reach of an authenticated caller too.
        anonymous = _create(client, "slow")
        assert (
            _error(client, "tasks/get", {"taskId": anonymous["taskId"]}, authorization=alice)["code"] == INVALID_PARAMS
        )
        _rpc(client, "tasks/cancel", {"taskId": anonymous["taskId"]})


def test_a_client_without_the_capability_gets_the_plain_result():
    api, _ = _build()
    with TestClient(api) as client:
        result = _call(client, "add", {"a": 1, "b": 2}, capabilities={})
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == {"sum": 3}

        created = _create(client, "add", {"a": 1, "b": 1})
        for method, params in (
            ("tasks/get", {"taskId": created["taskId"]}),
            ("tasks/update", {"taskId": created["taskId"], "inputResponses": {}}),
            ("tasks/cancel", {"taskId": created["taskId"]}),
        ):
            error = _error(client, method, params, capabilities={})
            assert error["code"] == MISSING_CLIENT_CAPABILITY, method
            assert TASKS_EXTENSION in error["data"]["requiredCapabilities"]["extensions"]


def test_a_legacy_client_gets_the_plain_result_also_when_it_declares_the_extension():
    api, _ = _build()
    with TestClient(api) as client:
        params = {**INITIALIZE_PARAMS, "capabilities": TASK_CAPS}
        resp = post_rpc(client, "initialize", params)
        assert resp.status_code == 200, resp.content
        session_id = resp.headers["mcp-session-id"]
        post_rpc(client, "notifications/initialized", id=None, session_id=session_id)
        resp = post_rpc(
            client, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, session_id=session_id, id=2
        )
        result = parse_rpc(resp)["result"]
        # SEP-2663: the extension does not apply before the 2026 revisions.
        assert "taskId" not in result
        assert result["structuredContent"] == {"sum": 5}


def test_a_tool_without_task_stays_synchronous_for_a_task_client():
    api, _ = _build()
    with TestClient(api) as client:
        result = _call(client, "plain")
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == {"task_id": None}


def test_elicit_in_a_task_suspends_until_tasks_update():
    api, probe = _build()
    with TestClient(api) as client:
        created = _create(client, "ask", capabilities=TASK_ELICIT_CAPS)
        task_id = created["taskId"]
        waiting = _poll(client, task_id, until=lambda r: r["status"] == "input_required", capabilities=TASK_ELICIT_CAPS)
        ((key, request),) = waiting["inputRequests"].items()
        assert request["method"] == "elicitation/create"
        assert request["params"]["message"] == "Proceed?"

        answer = {"action": "accept", "content": {"ok": True}}
        ack = _rpc(
            client,
            "tasks/update",
            {"taskId": task_id, "inputResponses": {"not-open": {}, key: answer}},
            capabilities=TASK_ELICIT_CAPS,
        )
        assert ack["result"] == {"resultType": "complete"}

        final = _poll(client, task_id, capabilities=TASK_ELICIT_CAPS)
        assert final["status"] == "completed"
        structured = final["result"]["structuredContent"]
        assert structured["answer"] == answer
        # The coroutine resumed where it waited: no MRTR replay from the top.
        assert structured["runs"] == 1
        assert probe.ask_runs == 1
        assert structured["task_id"] == task_id


def test_elicit_in_a_task_without_the_elicitation_capability_is_a_tool_error():
    api, _ = _build()
    with TestClient(api) as client:
        final = _poll(client, _create(client, "ask")["taskId"])
        assert final["status"] == "completed"
        assert final["result"]["isError"] is True
        assert "elicitation" in final["result"]["content"][0]["text"]


def test_cancel_wakes_a_task_that_waits_for_input():
    api, probe = _build()
    with TestClient(api) as client:
        task_id = _create(client, "ask", capabilities=TASK_ELICIT_CAPS)["taskId"]
        _poll(client, task_id, until=lambda r: r["status"] == "input_required", capabilities=TASK_ELICIT_CAPS)
        _rpc(client, "tasks/cancel", {"taskId": task_id}, capabilities=TASK_ELICIT_CAPS)
        final = _get(client, task_id, capabilities=TASK_ELICIT_CAPS)
        assert final["status"] == "cancelled"
        assert "inputRequests" not in final
        assert probe.cancelled["ask"].wait(5)


def test_progress_sets_the_status_message():
    api, _ = _build()
    with TestClient(api) as client:
        task_id = _create(client, "progress")["taskId"]
        result = _poll(client, task_id, until=lambda r: r.get("statusMessage") is not None)
        assert result["status"] == "working"
        assert result["statusMessage"] == "step one of four"
        _rpc(client, "tasks/cancel", {"taskId": task_id})


def test_many_tasks_run_at_once_and_each_keeps_its_own_result():
    api, _ = _build()
    finals: dict[int, dict] = {}
    errors: list[BaseException] = []
    with TestClient(api) as client:

        def run(first: int) -> None:
            try:
                # Create all tasks of this thread before the first poll, so they overlap.
                created = {n: _create(client, "add", {"a": n, "b": 1000}) for n in range(first, first + 5)}
                for n, task in created.items():
                    finals[n] = _poll(client, task["taskId"])
            except BaseException as exc:
                errors.append(exc)

        # Each thread sends its requests to its own test worker, as a connection does.
        threads = [threading.Thread(target=run, args=(first,)) for first in range(0, 40, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert not errors, errors
    assert len({final["taskId"] for final in finals.values()}) == 40
    for n, final in finals.items():
        assert final["status"] == "completed"
        assert final["result"]["structuredContent"] == {"sum": n + 1000}


def test_a_full_store_answers_with_a_tool_error():
    api, _ = _build(max_tasks=2)
    with TestClient(api) as client:
        first = _create(client, "slow")
        second = _create(client, "slow")
        refused = _call(client, "slow")
        assert refused["isError"] is True
        assert "taskId" not in refused
        assert "Too many tasks" in refused["content"][0]["text"]
        for created in (first, second):
            _rpc(client, "tasks/cancel", {"taskId": created["taskId"]})


def test_closing_the_client_cancels_running_tasks():
    api, probe = _build()
    with TestClient(api) as client:
        _create(client, "slow")
    assert probe.cancelled["slow"].wait(5), "a running task outlived its TestClient"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ttl_ms": 1000}, "task=True"),
        ({"poll_interval_ms": 10}, "task=True"),
        ({"task": True, "ttl_ms": 0}, "ttl_ms"),
        ({"task": True, "ttl_ms": True}, "ttl_ms"),
        ({"task": True, "poll_interval_ms": -1}, "poll_interval_ms"),
    ],
)
def test_task_options_are_checked(kwargs, message):
    mcp = MCP("bad")
    with pytest.raises(ValueError, match=message):

        @mcp.tool(**kwargs)
        async def t() -> dict:
            return {}


@pytest.mark.parametrize("max_tasks", [0, -1, True, 1.5])
def test_max_tasks_is_checked(max_tasks):
    with pytest.raises(ValueError, match="max_tasks"):
        MCP("bad", max_tasks=max_tasks)

"""Multi Round-Trip Requests (2026-07-28): elicitation via input_required + replay."""

from __future__ import annotations

from _helpers import _parse_all_sse, parse_rpc, post_rpc_modern
from bolt_mcp import MCP, Context, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

ELICIT_CAPS = {"elicitation": {}}


def _build():
    api = BoltAPI()
    mcp = MCP("mrtr-server")
    runs = {"count": 0}

    @mcp.tool
    async def confirm(item: str, ctx: Context) -> dict:
        runs["count"] += 1
        answer = await ctx.elicit(f"Really touch {item}?")
        return {"item": item, "answer": answer, "runs": runs["count"], "was_replay": ctx.is_replay}

    @mcp.tool
    async def two_steps(ctx: Context) -> dict:
        first = await ctx.elicit("First question?")
        second = await ctx.elicit("Second question?")
        return {"first": first, "second": second}

    @mcp.tool
    async def keyed(ctx: Context) -> dict:
        answer = await ctx.elicit("Named question?", key="the-answer")
        return {"answer": answer}

    mount_mcp(api, mcp)
    return api, runs


def _call(client, params, id=1):
    return post_rpc_modern(client, "tools/call", params, id=id, capabilities=ELICIT_CAPS)


def _accept(content=None):
    return {"action": "accept", "content": content or {}}


def test_elicit_returns_input_required_with_sealed_state():
    api, _ = _build()
    with TestClient(api) as client:
        resp = _call(client, {"name": "confirm", "arguments": {"item": "db"}})
        result = parse_rpc(resp)["result"]
        assert result["resultType"] == "input_required"
        assert result["requestState"]
        ((key, request),) = result["inputRequests"].items()
        assert request["method"] == "elicitation/create"
        assert "Really touch db?" in request["params"]["message"]
        assert key == "input-1"


def test_retry_with_answer_replays_and_completes():
    api, runs = _build()
    with TestClient(api) as client:
        resp = _call(client, {"name": "confirm", "arguments": {"item": "db"}})
        result = parse_rpc(resp)["result"]
        ((key, _),) = result["inputRequests"].items()

        resp = _call(
            client,
            {
                "name": "confirm",
                "arguments": {"item": "db"},
                "requestState": result["requestState"],
                "inputResponses": {key: _accept({"ok": True})},
            },
            id=2,
        )
        sc = parse_rpc(resp)["result"]["structuredContent"]
        assert sc["item"] == "db"
        assert sc["answer"]["action"] == "accept"
        assert sc["answer"]["content"] == {"ok": True}
        # The tool re-ran from the top — replay semantics, not resumption.
        assert sc["runs"] == 2
        assert sc["was_replay"] is True


def test_sequential_elicits_accumulate_across_round_trips():
    api, _ = _build()
    with TestClient(api) as client:
        # Round 1: first elicit fires.
        r1 = parse_rpc(_call(client, {"name": "two_steps", "arguments": {}}))["result"]
        ((k1, q1),) = r1["inputRequests"].items()
        assert "First" in q1["params"]["message"]

        # Round 2: first answered -> second elicit fires, state accumulates.
        r2 = parse_rpc(
            _call(
                client,
                {
                    "name": "two_steps",
                    "arguments": {},
                    "requestState": r1["requestState"],
                    "inputResponses": {k1: _accept({"a": 1})},
                },
                id=2,
            )
        )["result"]
        assert r2["resultType"] == "input_required"
        ((k2, q2),) = r2["inputRequests"].items()
        assert "Second" in q2["params"]["message"]
        assert k2 != k1

        # Round 3: both answered -> final result sees both answers.
        r3 = parse_rpc(
            _call(
                client,
                {
                    "name": "two_steps",
                    "arguments": {},
                    "requestState": r2["requestState"],
                    "inputResponses": {k2: _accept({"b": 2})},
                },
                id=3,
            )
        )["result"]
        sc = r3["structuredContent"]
        assert sc["first"]["content"] == {"a": 1}
        assert sc["second"]["content"] == {"b": 2}


def test_explicit_key_names_the_input_point():
    api, _ = _build()
    with TestClient(api) as client:
        result = parse_rpc(_call(client, {"name": "keyed", "arguments": {}}))["result"]
        assert list(result["inputRequests"]) == ["the-answer"]


def test_tampered_request_state_rejected_in_band():
    api, _ = _build()
    with TestClient(api) as client:
        result = parse_rpc(_call(client, {"name": "confirm", "arguments": {"item": "db"}}))["result"]
        ((key, _),) = result["inputRequests"].items()
        resp = _call(
            client,
            {
                "name": "confirm",
                "arguments": {"item": "db"},
                "requestState": result["requestState"] + "x",
                "inputResponses": {key: _accept()},
            },
            id=2,
        )
        final = parse_rpc(resp)["result"]
        assert final["isError"] is True
        assert "requestState" in final["content"][0]["text"]


def test_request_state_bound_to_arguments():
    # Anti-replay: sealed state carries an arguments digest; a retry with
    # different arguments must be rejected.
    api, _ = _build()
    with TestClient(api) as client:
        result = parse_rpc(_call(client, {"name": "confirm", "arguments": {"item": "db"}}))["result"]
        ((key, _),) = result["inputRequests"].items()
        resp = _call(
            client,
            {
                "name": "confirm",
                "arguments": {"item": "OTHER"},
                "requestState": result["requestState"],
                "inputResponses": {key: _accept()},
            },
            id=2,
        )
        final = parse_rpc(resp)["result"]
        assert final["isError"] is True


def test_request_state_bound_to_tool():
    api, _ = _build()
    with TestClient(api) as client:
        result = parse_rpc(_call(client, {"name": "keyed", "arguments": {}}))["result"]
        resp = _call(
            client,
            {
                "name": "two_steps",
                "arguments": {},
                "requestState": result["requestState"],
                "inputResponses": {"the-answer": _accept()},
            },
            id=2,
        )
        final = parse_rpc(resp)["result"]
        assert final["isError"] is True


def test_user_except_blocks_do_not_swallow_input_required():
    api = BoltAPI()
    mcp = MCP("greedy-except")

    @mcp.tool
    async def greedy(ctx: Context) -> dict:
        try:
            answer = await ctx.elicit("Q?")
        except Exception:  # noqa: BLE001 — the point: broad except must not eat MRTR
            return {"swallowed": True}
        return {"answer": answer}

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        result = parse_rpc(post_rpc_modern(client, "tools/call", {"name": "greedy", "arguments": {}}, capabilities=ELICIT_CAPS))[
            "result"
        ]
        assert result["resultType"] == "input_required"


def test_notifications_stream_before_input_required():
    api = BoltAPI()
    mcp = MCP("notify-then-ask")

    @mcp.tool
    async def noisy(ctx: Context) -> dict:
        await ctx.report_progress(0.5, 1.0)
        answer = await ctx.elicit("Q?")
        return {"answer": answer}

    mount_mcp(api, mcp)
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client,
            "tools/call",
            {"name": "noisy", "arguments": {}},
            capabilities=ELICIT_CAPS,
            progress_token="tok",
        )
        events = _parse_all_sse(resp.content)
        methods = [e.get("method") for e in events if e.get("method")]
        assert "notifications/progress" in methods
        assert events[-1]["result"]["resultType"] == "input_required"

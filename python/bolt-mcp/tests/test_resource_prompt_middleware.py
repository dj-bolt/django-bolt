"""resources/read and prompts/get — middleware with the request ``_meta`` and principal."""

from __future__ import annotations

import pytest
from _helpers import initialize, mint_jwt, parse_rpc, post_rpc
from bolt_mcp import MCP, PromptGet, ResourceRead, mount_mcp

from django_bolt import BoltAPI, IsAuthenticated, JWTAuthentication
from django_bolt.testing import TestClient

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SECRET = "middleware-secret-key-0123456789-abcd"


def _server(resource_middleware=(), prompt_middleware=(), **mount_kwargs):
    api = BoltAPI()
    mcp = MCP("middleware-test")
    for fn in resource_middleware:
        mcp.resource_middleware(fn)
    for fn in prompt_middleware:
        mcp.prompt_middleware(fn)

    @mcp.resource("config://app", name="app-config", mime_type="application/json")
    def app_config() -> str:
        return '{"env": "test"}'

    @mcp.resource("users://{user_id}/profile", name="user-profile")
    def user_profile(user_id: int) -> str:
        return f"user {user_id}"

    @mcp.prompt
    def greet(name: str) -> str:
        return f"Hello {name}"

    mount_mcp(api, mcp, path="/mcp", **mount_kwargs)
    return api


def _rpc(client, method: str, params: dict, meta: dict | None = None) -> dict:
    _, session_id = initialize(client)
    if meta is not None:
        params = {**params, "_meta": meta}
    return parse_rpc(post_rpc(client, method, params, session_id=session_id))


def _recorder():
    seen: list = []
    results: list[dict] = []

    async def record(request, call_next) -> dict:
        seen.append(request)
        result = await call_next()
        results.append(result)
        return result

    return record, seen, results


def test_resource_middleware_gets_the_uri_and_the_request_meta():
    record, seen, results = _recorder()
    with TestClient(_server(resource_middleware=[record])) as client:
        body = _rpc(client, "resources/read", {"uri": "config://app"}, meta={"traceparent": TRACEPARENT})
    assert body["result"]["contents"][0]["text"] == '{"env": "test"}'
    assert seen == [ResourceRead(uri="config://app", meta={"traceparent": TRACEPARENT}, principal={})]
    assert results[0]["contents"][0]["uri"] == "config://app"


def test_resource_middleware_sees_an_empty_meta_when_the_client_sends_none():
    record, seen, _ = _recorder()
    with TestClient(_server(resource_middleware=[record])) as client:
        _rpc(client, "resources/read", {"uri": "users://7/profile"})
    assert seen[0].uri == "users://7/profile"
    assert seen[0].meta == {}


def test_resource_middleware_can_replace_the_result():
    async def replace(read: ResourceRead, call_next) -> dict:
        result = await call_next()
        result["contents"][0]["text"] = "replaced"
        return result

    with TestClient(_server(resource_middleware=[replace])) as client:
        body = _rpc(client, "resources/read", {"uri": "config://app"})
    assert body["result"]["contents"][0]["text"] == "replaced"


def test_unknown_resource_raises_through_the_middleware_and_stays_a_jsonrpc_error():
    raised: list[BaseException] = []

    async def watch(read: ResourceRead, call_next) -> dict:
        try:
            return await call_next()
        except Exception as exc:
            raised.append(exc)
            raise

    with TestClient(_server(resource_middleware=[watch])) as client:
        body = _rpc(client, "resources/read", {"uri": "config://nope"})
    assert body["error"]["code"] == -32002
    assert "config://nope" in body["error"]["message"]
    assert len(raised) == 1


def test_prompt_middleware_gets_the_name_arguments_and_meta():
    record, seen, results = _recorder()
    with TestClient(_server(prompt_middleware=[record])) as client:
        body = _rpc(
            client, "prompts/get", {"name": "greet", "arguments": {"name": "Ada"}}, meta={"traceparent": TRACEPARENT}
        )
    assert body["result"]["messages"][0]["content"]["text"] == "Hello Ada"
    assert seen == [PromptGet(name="greet", arguments={"name": "Ada"}, meta={"traceparent": TRACEPARENT}, principal={})]
    assert results[0]["messages"][0]["content"]["text"] == "Hello Ada"


def test_prompt_middleware_runs_in_registration_order():
    order: list[str] = []

    def named(label: str):
        async def middleware(get: PromptGet, call_next) -> dict:
            order.append(f"{label} in")
            result = await call_next()
            order.append(f"{label} out")
            return result

        return middleware

    with TestClient(_server(prompt_middleware=[named("outer"), named("inner")])) as client:
        _rpc(client, "prompts/get", {"name": "greet", "arguments": {"name": "Ada"}})
    assert order == ["outer in", "inner in", "inner out", "outer out"]


def test_invalid_prompt_arguments_stay_a_jsonrpc_error_under_middleware():
    record, seen, results = _recorder()
    with TestClient(_server(prompt_middleware=[record])) as client:
        body = _rpc(client, "prompts/get", {"name": "greet", "arguments": {}})
    assert body["error"]["code"] == -32602
    assert len(seen) == 1
    assert results == []


def test_resource_and_prompt_middleware_get_the_principal():
    record_read, reads, _ = _recorder()
    record_get, gets, _ = _recorder()
    api = _server(
        resource_middleware=[record_read],
        prompt_middleware=[record_get],
        auth=[JWTAuthentication(secret=SECRET)],
        guards=[IsAuthenticated()],
    )
    bearer = f"Bearer {mint_jwt(SECRET, sub='42')}"
    with TestClient(api) as client:
        _, session_id = initialize(client, authorization=bearer)
        for method, params in [
            ("resources/read", {"uri": "config://app"}),
            ("prompts/get", {"name": "greet", "arguments": {"name": "Ada"}}),
        ]:
            resp = post_rpc(client, method, params, session_id=session_id, authorization=bearer)
            assert "result" in parse_rpc(resp)
    assert reads[0].principal["user_id"] == "42"
    assert gets[0].principal["user_id"] == "42"


@pytest.mark.parametrize("decorator", ["resource_middleware", "prompt_middleware"])
def test_sync_middleware_is_rejected(decorator):
    mcp = MCP("sync")

    def not_async(request, call_next):
        return call_next()

    with pytest.raises(TypeError, match="must be an async function"):
        getattr(mcp, decorator)(not_async)

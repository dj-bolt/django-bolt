"""A tool call of an exposed route gives the body that HTTP gives for the route (#957).

Each test calls the route over HTTP and over MCP ``tools/call`` with ``TestClient``,
and compares the two. The ``response_model`` and the return annotation apply to both.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import msgspec
import pytest
from _helpers import parse_rpc, post_rpc_modern
from bolt_mcp import MCP, expose_routes
from django.contrib.auth.models import Group

from django_bolt import JSON, BoltAPI, StreamingResponse
from django_bolt.responses import PlainText, Redirect
from django_bolt.testing import TestClient


class Public(msgspec.Struct):
    name: str


class GroupOut(msgspec.Struct):
    name: str


class Missing(msgspec.Struct):
    detail: str


SECRET_ROW = {"name": "a", "password_hash": "SECRET"}


def _call(client, name: str, arguments: dict | None = None) -> dict:
    resp = post_rpc_modern(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert resp.status_code == 200
    return parse_rpc(resp)["result"]


def _tools(client) -> dict[str, dict]:
    resp = post_rpc_modern(client, "tools/list")
    assert resp.status_code == 200
    return {tool["name"]: tool for tool in parse_rpc(resp)["result"]["tools"]}


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True, "resultType": "complete"}


def _assert_same_as_http(result: dict, http_resp) -> None:
    """The tool result holds the HTTP body: the same text and the same JSON."""
    assert http_resp.status_code == 200
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": http_resp.content.decode()}]
    body = msgspec.json.decode(http_resp.content)
    assert result["structuredContent"] == (body if isinstance(body, dict) else {"result": body})
    assert "SECRET" not in http_resp.content.decode()


def test_response_model_strips_fields_over_mcp_as_over_http():
    """The reproduction of #957: the field that HTTP strips does not leak over MCP."""
    api = BoltAPI()

    @api.get("/user", response_model=Public)
    async def user() -> dict:
        return dict(SECRET_ROW)

    api.mount_mcp(MCP("t"), expose=[user])
    with TestClient(api) as client:
        http_resp = client.get("/user")
        result = _call(client, "user")
    assert http_resp.json() == {"name": "a"}
    _assert_same_as_http(result, http_resp)


def test_return_annotation_strips_fields_for_a_sync_handler():
    api = BoltAPI()

    @api.get("/user")
    def user() -> Public:
        return dict(SECRET_ROW)  # type: ignore[return-value]

    api.mount_mcp(MCP("t"), expose=[user])
    with TestClient(api) as client:
        _assert_same_as_http(_call(client, "user"), client.get("/user"))


def test_list_annotation_strips_fields_of_each_item():
    api = BoltAPI()

    @api.get("/users")
    async def users() -> list[Public]:
        return [dict(SECRET_ROW), {"name": "b", "password_hash": "SECRET"}]

    api.mount_mcp(MCP("t"), expose=[users])
    with TestClient(api) as client:
        http_resp = client.get("/users")
        result = _call(client, "users")
    assert http_resp.json() == [{"name": "a"}, {"name": "b"}]
    _assert_same_as_http(result, http_resp)


@pytest.mark.parametrize("is_async", [True, False], ids=["async", "sync"])
def test_queryset_return_gives_the_http_body(is_async):
    prefix = f"g957-{uuid.uuid4().hex[:8]}-"
    Group.objects.bulk_create([Group(name=f"{prefix}1"), Group(name=f"{prefix}2")])
    api = BoltAPI()
    if is_async:

        @api.get("/groups")
        async def groups() -> list[GroupOut]:
            return Group.objects.filter(name__startswith=prefix).order_by("name")

    else:

        @api.get("/groups")
        def groups() -> list[GroupOut]:
            return Group.objects.filter(name__startswith=prefix).order_by("name")

    api.mount_mcp(MCP("t"), expose=[groups])
    try:
        with TestClient(api) as client:
            http_resp = client.get("/groups")
            result = _call(client, "groups")
    finally:
        Group.objects.filter(name__startswith=prefix).delete()
    assert http_resp.json() == [{"name": f"{prefix}1"}, {"name": f"{prefix}2"}]
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": http_resp.content.decode()}]
    assert result["structuredContent"] == {"result": http_resp.json()}


@pytest.mark.parametrize("mask", [True, False], ids=["masked", "unmasked"])
def test_a_value_that_fails_response_validation_gives_an_error_result(mask):
    api = BoltAPI()

    @api.get("/user", response_model=Public)
    async def user() -> dict:
        return {"password_hash": "SECRET"}

    api.mount_mcp(MCP("t", mask_error_details=mask), expose=[user])
    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.get("/user").status_code == 500
        result = _call(client, "user")
    assert result["isError"] is True
    assert "structuredContent" not in result
    (content,) = result["content"]
    assert "SECRET" not in content["text"]
    if mask:
        assert content["text"] == "Tool 'user' failed."
    else:
        assert "name" in content["text"]


def test_validate_response_false_gives_the_unvalidated_http_body():
    api = BoltAPI()

    @api.get("/user", response_model=Public, validate_response=False)
    async def user() -> dict:
        return {"name": "a", "extra": 1}

    api.mount_mcp(MCP("t"), expose=[user])
    with TestClient(api) as client:
        http_resp = client.get("/user")
        result = _call(client, "user")
        tools = _tools(client)
    assert http_resp.json() == {"name": "a", "extra": 1}
    _assert_same_as_http(result, http_resp)
    # HTTP does not check the body, so the tool does not promise a shape.
    assert "outputSchema" not in tools["user"]


def test_multi_response_route_gives_the_body_of_each_status():
    api = BoltAPI()

    @api.get("/user/{uid}", response_model={200: Public, 404: Missing})
    async def user(uid: int):
        if uid == 0:
            return 404, {"detail": "no user"}
        return Public(name="a")

    api.mount_mcp(MCP("t"), expose=[user])
    with TestClient(api) as client:
        found_http = client.get("/user/1")
        found = _call(client, "user", {"uid": 1})
        missing_http = client.get("/user/0")
        missing = _call(client, "user", {"uid": 0})
    _assert_same_as_http(found, found_http)
    assert missing_http.status_code == 404
    assert missing == _error(missing_http.content.decode())


def test_response_object_with_an_error_status_gives_an_error_result():
    api = BoltAPI()

    @api.get("/user", response_model=Public)
    async def user():
        return JSON({"detail": "gone"}, status_code=410, headers={"X-Trace": "1"})

    api.mount_mcp(MCP("t"), expose=[user])
    with TestClient(api) as client:
        http_resp = client.get("/user")
        result = _call(client, "user")
    assert http_resp.status_code == 410
    assert result == _error(http_resp.content.decode())


def test_redirect_gives_an_error_result():
    api = BoltAPI()

    @api.get("/old")
    async def old():
        return Redirect("/new")

    api.mount_mcp(MCP("t"), expose=[old])
    with TestClient(api) as client:
        result = _call(client, "old")
    assert result == _error("Tool 'old' answered with HTTP status 307.")


def test_plain_text_gives_text_content_only():
    api = BoltAPI()

    @api.get("/hello")
    async def hello():
        return PlainText("hi")

    api.mount_mcp(MCP("t"), expose=[hello])
    with TestClient(api) as client:
        assert client.get("/hello").text == "hi"
        result = _call(client, "hello")
    assert result == {"content": [{"type": "text", "text": "hi"}], "isError": False, "resultType": "complete"}


def test_empty_response_gives_no_content():
    api = BoltAPI()

    @api.post("/ping", status_code=204)
    async def ping() -> None:
        return None

    api.mount_mcp(MCP("t"), expose=[ping])
    with TestClient(api) as client:
        assert client.post("/ping").status_code == 204
        result = _call(client, "ping")
    assert result == {"content": [], "isError": False, "resultType": "complete"}


def test_binary_body_gives_an_error_result():
    api = BoltAPI()

    @api.get("/blob")
    async def blob() -> bytes:
        return b"\x00\x01"

    api.mount_mcp(MCP("t", mask_error_details=False), expose=[blob])
    with TestClient(api) as client:
        result = _call(client, "blob")
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "Tool 'blob' returned a body of type 'application/octet-stream'. A tool result holds only JSON or text."
    )


def test_streaming_response_returned_at_run_time_gives_an_error_result():
    api = BoltAPI()

    async def chunks():
        yield b"a"

    @api.get("/events")
    async def events() -> dict:
        return StreamingResponse(chunks())  # type: ignore[return-value]

    api.mount_mcp(MCP("t", mask_error_details=False), expose=[events])
    with TestClient(api) as client:
        result = _call(client, "events")
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "Tool 'events' returned a streaming or file response. A tool result cannot hold one."
    )


def test_a_stream_route_is_refused_at_mount():
    api = BoltAPI()

    @api.get("/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(iter([b"a"]))

    with pytest.raises(ValueError, match="returns a streaming or file response"):
        api.mount_mcp(MCP("t"), expose=[events])


def test_a_generator_route_is_refused_at_mount():
    api = BoltAPI()

    @api.get("/events")
    async def events() -> AsyncIterator[str]:
        yield "a"

    with pytest.raises(ValueError, match="returns a streaming or file response"):
        api.mount_mcp(MCP("t"), expose=[events])


def test_output_schema_is_the_schema_of_the_validated_struct():
    api = BoltAPI()

    @api.get("/user", response_model=Public)
    async def user() -> dict:
        return dict(SECRET_ROW)

    @api.get("/users")
    async def users() -> list[Public]:
        return []

    @api.get("/raw")
    async def raw() -> dict:
        return {}

    api.mount_mcp(MCP("t"), expose=[user, users, raw])
    with TestClient(api) as client:
        tools = _tools(client)
        result = _call(client, "user")
    assert tools["user"]["outputSchema"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    # structuredContent of the call holds only what the schema describes.
    assert set(result["structuredContent"]) <= set(tools["user"]["outputSchema"]["properties"])
    # A list body goes in {"result": ...}, and a dict body has no fixed shape.
    assert "outputSchema" not in tools["users"]
    assert "outputSchema" not in tools["raw"]


def test_a_body_that_the_output_schema_does_not_describe_gives_an_error_result():
    """The tool advertises the schema of Public, so a text body cannot be a success result."""
    api = BoltAPI()

    @api.get("/user", response_model=Public)
    async def user():
        return PlainText("a")

    api.mount_mcp(MCP("t", mask_error_details=False), expose=[user])
    with TestClient(api) as client:
        assert client.get("/user").text == "a"
        result = _call(client, "user")
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "Tool 'user' returned a body that its output schema does not describe (status 200, 'text/plain')."
    )


def test_mcp_tool_result_does_not_change():
    """An @mcp.tool is not a route: its return value goes to the client as it is."""
    api = BoltAPI()
    mcp = MCP("t")

    @mcp.tool
    async def row() -> dict:
        return {"name": "a", "extra": 1}

    api.mount_mcp(mcp)
    with TestClient(api) as client:
        result = _call(client, "row")
        tools = _tools(client)
    assert result["structuredContent"] == {"name": "a", "extra": 1}
    assert "outputSchema" not in tools["row"]


def test_expose_routes_with_patterns_skips_a_stream_route():
    """A bulk selection skips a route that a tool cannot serve, as it does for a file or form route."""
    api = BoltAPI()
    mcp = MCP("t")

    @api.get("/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(iter([b"a"]))

    @api.get("/user", response_model=Public)
    async def user() -> dict:
        return dict(SECRET_ROW)

    expose_routes(mcp, api, only_marked=False)
    api.mount_mcp(mcp)
    with TestClient(api) as client:
        assert set(_tools(client)) == {"user"}
        assert _call(client, "user")["structuredContent"] == {"name": "a"}

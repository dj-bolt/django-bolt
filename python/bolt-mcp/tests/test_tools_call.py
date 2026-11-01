"""tools/call — execution, result mapping, and error semantics."""

from __future__ import annotations

import json
from typing import Any

import msgspec
import pytest
from _helpers import initialize, make_server, parse_rpc, post_rpc
from bolt_mcp import MCP, expose_as_tool, expose_routes, mount_mcp

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, computed_field, field
from django_bolt.testing import TestClient

SECRET = "s3cret-value"


class Account(Serializer):
    id: int
    password: str = field(write_only=True)

    @computed_field
    def label(self) -> str:
        return f"user-{self.id}"


class Holder(msgspec.Struct):
    account: Any


DUMPED = {"id": 1, "label": "user-1"}


def _call(client, session_id, name, arguments, *, id=1):
    return post_rpc(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        id=id,
        session_id=session_id,
    )


def test_dict_result_maps_to_text_and_structured_content():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, "add", {"a": 2, "b": 3})
        assert resp.status_code == 200
        result = parse_rpc(resp)["result"]
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert result["structuredContent"] == {"sum": 5}


def test_string_result_maps_to_text_content():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, "shout", {"text": "hi"})
        result = parse_rpc(resp)["result"]
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "HI"


def test_invalid_arguments_are_in_band_error_not_jsonrpc_error():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, "add", {"a": "not-an-int", "b": 3})
        assert resp.status_code == 200
        body = parse_rpc(resp)
        assert "error" not in body, "argument validation failures are in-band CallToolResult errors"
        assert body["result"]["isError"] is True


def test_tool_exception_is_in_band_error():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, "boom", {})
        body = parse_rpc(resp)
        assert "error" not in body
        assert body["result"]["isError"] is True


def test_unknown_tool_returns_in_band_error():
    # MCP semantics (per FastMCP / the SDK): an unknown tool name inside a valid
    # tools/call is a tool-level failure → in-band CallToolResult(isError=true),
    # NOT a JSON-RPC error. (An unknown *method* is the JSON-RPC error case.)
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, "nope", {})
        body = parse_rpc(resp)
        assert "error" not in body
        assert body["result"]["isError"] is True


def test_request_id_echoed_verbatim_for_int_and_string():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        assert parse_rpc(_call(client, session_id, "add", {"a": 1, "b": 1}, id=7))["id"] == 7
        assert parse_rpc(_call(client, session_id, "add", {"a": 1, "b": 1}, id="abc"))["id"] == "abc"


def test_json_response_mode_returns_application_json():
    # MCP(json_response=True) returns a single application/json object instead of
    # an SSE stream — the multi-process-friendly mode.
    api, _ = make_server(json_response=True)
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, "add", {"a": 10, "b": 20})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["result"]["structuredContent"] == {"sum": 30}


def _serializer_server():
    api = BoltAPI()
    mcp = MCP("serializer-server")

    @api.get("/accounts/{account_id}")
    @expose_as_tool(name="get_account")
    def get_account(account_id: int) -> Account:
        return Account(id=account_id, password=SECRET)

    @api.get("/accounts-as-dicts", response_model=list[Account])
    @expose_as_tool(name="list_account_dicts")
    def list_account_dicts():
        return [{"id": 1, "password": SECRET}]

    @api.get("/account-as-dict/{account_id}")
    @expose_as_tool(name="get_account_dict")
    async def get_account_dict(account_id: int) -> Account:
        return {"id": account_id, "password": SECRET}

    @mcp.tool
    async def list_accounts() -> list[Account]:
        """List the accounts."""
        return [Account(id=1, password=SECRET)]

    @mcp.tool
    def hold_account() -> Holder:
        """Return a Struct that holds an account."""
        return Holder(account=Account(id=1, password=SECRET))

    expose_routes(mcp, api)
    mount_mcp(api, mcp)
    return api


@pytest.mark.parametrize(
    ("name", "arguments", "payload", "structured"),
    [
        ("get_account", {"account_id": 1}, DUMPED, DUMPED),
        ("list_accounts", {}, [DUMPED], {"result": [DUMPED]}),
        ("hold_account", {}, {"account": DUMPED}, {"account": DUMPED}),
        ("list_account_dicts", {}, [DUMPED], {"result": [DUMPED]}),
        ("get_account_dict", {"account_id": 1}, DUMPED, DUMPED),
    ],
    ids=["serializer", "list", "struct", "route-response-model-sync", "route-return-type-async"],
)
def test_a_serializer_in_a_tool_result_gives_its_dump(name, arguments, payload, structured):
    """A tool result gives the dump of each Serializer, as the HTTP route does."""
    with TestClient(_serializer_server()) as client:
        _, session_id = initialize(client)
        resp = _call(client, session_id, name, arguments)
        assert resp.status_code == 200
        assert SECRET not in resp.text
        result = parse_rpc(resp)["result"]
        assert result["isError"] is False
        assert result["structuredContent"] == structured
        assert json.loads(result["content"][0]["text"]) == payload

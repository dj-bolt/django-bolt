"""tools/list — tool metadata and JSON Schema generation."""

from __future__ import annotations

from _helpers import initialize, make_server, parse_rpc, post_rpc

from django_bolt.testing import TestClient


def _list_tools(client, session_id):
    resp = post_rpc(client, "tools/list", session_id=session_id)
    assert resp.status_code == 200
    tools = parse_rpc(resp)["result"]["tools"]
    return {t["name"]: t for t in tools}


def test_tools_list_exposes_registered_tools():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        tools = _list_tools(client, session_id)
        assert {"greet", "add", "shout", "boom"} <= set(tools)
        # description comes from the explicit kwarg or the docstring
        assert tools["add"]["description"] == "Add two integers"
        assert "Greet someone by name." in tools["greet"]["description"]


def test_input_schema_is_object_with_properties_and_required():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        tools = _list_tools(client, session_id)

        greet_schema = tools["greet"]["inputSchema"]
        assert greet_schema["type"] == "object"
        assert greet_schema["properties"]["name"]["type"] == "string"
        assert greet_schema["required"] == ["name"]

        add_schema = tools["add"]["inputSchema"]
        assert add_schema["type"] == "object"
        assert add_schema["properties"]["a"]["type"] == "integer"
        assert add_schema["properties"]["b"]["type"] == "integer"
        assert set(add_schema["required"]) == {"a", "b"}


def test_no_arg_tool_has_empty_object_schema():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        tools = _list_tools(client, session_id)
        boom_schema = tools["boom"]["inputSchema"]
        assert boom_schema["type"] == "object"
        assert boom_schema.get("properties", {}) == {}

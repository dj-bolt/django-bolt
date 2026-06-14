"""resources/* and prompts/* round-trips."""

from __future__ import annotations

import pytest
from _helpers import initialize, make_server, parse_rpc, post_rpc
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def test_resources_list_and_read():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)

        listed = post_rpc(client, "resources/list", session_id=session_id)
        assert listed.status_code == 200
        resources = {r["uri"]: r for r in parse_rpc(listed)["result"]["resources"]}
        assert "config://app" in resources
        assert resources["config://app"]["mimeType"] == "application/json"

        read = post_rpc(client, "resources/read", {"uri": "config://app"}, session_id=session_id)
        assert read.status_code == 200
        contents = parse_rpc(read)["result"]["contents"]
        assert contents[0]["uri"] == "config://app"
        assert contents[0]["text"] == '{"env": "test"}'


def test_resource_templates_list():
    # Clients (Claude, Inspector) call resources/templates/list during discovery;
    # with no registered templates it must still return a well-formed empty list.
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "resources/templates/list", session_id=session_id)
        assert resp.status_code == 200
        assert parse_rpc(resp)["result"]["resourceTemplates"] == []


def _template_server():
    """A server exposing one parameterized (templated) resource."""
    api = BoltAPI()
    mcp = MCP("tmpl", "1.0.0")

    @mcp.resource(
        "users://{user_id}/profile",
        name="user-profile",
        mime_type="application/json",
        description="A user's profile document",
    )
    async def user_profile(user_id: int) -> str:
        # user_id must arrive coerced to int — it is extracted from the URI as a string.
        return f'{{"id": {user_id}, "type": "{type(user_id).__name__}"}}'

    mount_mcp(api, mcp)
    return api


def test_resource_template_listed():
    with TestClient(_template_server()) as client:
        _, session_id = initialize(client)
        resp = post_rpc(client, "resources/templates/list", session_id=session_id)
        assert resp.status_code == 200
        templates = parse_rpc(resp)["result"]["resourceTemplates"]
        assert len(templates) == 1
        tmpl = templates[0]
        assert tmpl["uriTemplate"] == "users://{user_id}/profile"
        assert tmpl["name"] == "user-profile"
        assert tmpl["mimeType"] == "application/json"
        assert tmpl["description"] == "A user's profile document"
        # A templated resource is NOT a static resource.
        listed = post_rpc(client, "resources/list", session_id=session_id)
        assert parse_rpc(listed)["result"]["resources"] == []


def test_resource_template_read_extracts_and_coerces_params():
    with TestClient(_template_server()) as client:
        _, session_id = initialize(client)
        read = post_rpc(client, "resources/read", {"uri": "users://42/profile"}, session_id=session_id)
        assert read.status_code == 200
        contents = parse_rpc(read)["result"]["contents"]
        assert contents[0]["uri"] == "users://42/profile"
        assert contents[0]["mimeType"] == "application/json"
        # {user_id} extracted from the URI and coerced from "42" (str) to int.
        assert contents[0]["text"] == '{"id": 42, "type": "int"}'


def test_resource_template_non_matching_uri_is_error():
    with TestClient(_template_server()) as client:
        _, session_id = initialize(client)
        # Does not match users://{user_id}/profile (wrong trailing segment).
        read = post_rpc(client, "resources/read", {"uri": "users://7/settings"}, session_id=session_id)
        assert "error" in parse_rpc(read)


def test_resource_template_uncoercible_param_is_error():
    with TestClient(_template_server()) as client:
        _, session_id = initialize(client)
        # Matches the template, but "abc" cannot coerce to int → invalid params.
        read = post_rpc(client, "resources/read", {"uri": "users://abc/profile"}, session_id=session_id)
        assert "error" in parse_rpc(read)


def test_resource_template_param_mismatch_raises_at_registration():
    mcp = MCP("bad", "1.0.0")
    with pytest.raises(ValueError, match="do not match"):

        @mcp.resource("docs://{doc_id}")
        async def get_doc(wrong_name: str) -> str:
            return wrong_name


def test_prompts_list_and_get():
    api, _ = make_server()
    with TestClient(api) as client:
        _, session_id = initialize(client)

        listed = post_rpc(client, "prompts/list", session_id=session_id)
        assert listed.status_code == 200
        prompts = {p["name"]: p for p in parse_rpc(listed)["result"]["prompts"]}
        assert "summarize" in prompts
        arg_names = {a["name"] for a in prompts["summarize"].get("arguments", [])}
        assert "topic" in arg_names

        got = post_rpc(
            client,
            "prompts/get",
            {"name": "summarize", "arguments": {"topic": "otters"}},
            session_id=session_id,
        )
        assert got.status_code == 200
        messages = parse_rpc(got)["result"]["messages"]
        assert messages[0]["role"] in ("user", "assistant")
        assert "otters" in messages[0]["content"]["text"]

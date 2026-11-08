"""resources/read — ``_meta`` on the contents."""

from __future__ import annotations

from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

APP_META = {"ui": {"csp": {"connectDomains": ["https://api.example.com"]}, "prefersBorder": True}}


def _server():
    api = BoltAPI()
    mcp = MCP("resource-meta-test")

    @mcp.resource("ui://weather/view.html", mime_type="text/html;profile=mcp-app", meta=APP_META)
    def weather_view() -> str:
        return "<html></html>"

    @mcp.resource("ui://cities/{city}/view.html", mime_type="text/html;profile=mcp-app", meta=APP_META)
    def city_view(city: str) -> str:
        return f"<html>{city}</html>"

    @mcp.resource("config://plain")
    def plain() -> str:
        return "plain"

    mount_mcp(api, mcp, path="/mcp")
    return api


def _read(client, uri: str) -> dict:
    _, session_id = initialize(client)
    resp = post_rpc(client, "resources/read", {"uri": uri}, session_id=session_id)
    return parse_rpc(resp)["result"]["contents"][0]


def test_resource_meta_is_on_the_contents():
    with TestClient(_server()) as client:
        content = _read(client, "ui://weather/view.html")
        assert content["_meta"] == APP_META
        assert content["mimeType"] == "text/html;profile=mcp-app"


def test_resource_template_meta_is_on_the_contents():
    with TestClient(_server()) as client:
        content = _read(client, "ui://cities/leeds/view.html")
        assert content["_meta"] == APP_META
        assert content["text"] == "<html>leeds</html>"


def test_resource_without_meta_has_no_meta_key():
    with TestClient(_server()) as client:
        assert "_meta" not in _read(client, "config://plain")

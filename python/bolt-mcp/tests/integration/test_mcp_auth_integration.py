"""Tier 1 auth enforced by Rust over a real runbolt server."""

from __future__ import annotations

import time

import jwt
import msgspec
import pytest

pytestmark = pytest.mark.server_integration

DUAL_ACCEPT = "application/json, text/event-stream"
SECRET = "itest-auth-secret-0123456789-abcdefgh"

MCP_API_BODY = f"""
from django_bolt import JWTAuthentication, IsAuthenticated
from bolt_mcp import MCP, mount_mcp

mcp = MCP("auth-itest", "1.0")


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {{"sum": a + b}}


mount_mcp(api, mcp, auth=[JWTAuthentication(secret="{SECRET}")], guards=[IsAuthenticated()])
"""


def _headers(*, authorization=None):
    h = {"Accept": DUAL_ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    if authorization:
        h["Authorization"] = authorization
    return h


def _initialize_body():
    return msgspec.json.encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "it", "version": "1"}},
        }
    )


def test_missing_token_rejected_by_rust(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    with project.start() as server:
        resp = server.client.post(server.url("/mcp"), content=_initialize_body(), headers=_headers())
        assert resp.status_code == 401


def test_valid_token_accepted(make_server_project):
    project = make_server_project(project_api_body=MCP_API_BODY)
    token = jwt.encode({"sub": "7", "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    with project.start() as server:
        resp = server.client.post(
            server.url("/mcp"), content=_initialize_body(), headers=_headers(authorization=f"Bearer {token}")
        )
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id")

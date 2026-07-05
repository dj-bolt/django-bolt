"""Tier 1 auth enforced by Rust over a real runbolt server."""

from __future__ import annotations

import time

import jwt
import pytest
from _helpers import INITIALIZE_PARAMS, mcp_app_source, mcp_headers, rpc_body

pytestmark = pytest.mark.server_integration

SECRET = "itest-auth-secret-0123456789-abcdefgh"


def test_missing_token_rejected_by_rust(make_server_project):
    project = make_server_project(api_source=mcp_app_source("auth"))
    with project.start() as server:
        resp = server.client.post(
            server.url("/mcp"), content=rpc_body("initialize", INITIALIZE_PARAMS), headers=mcp_headers()
        )
        assert resp.status_code == 401


def test_valid_token_accepted(make_server_project):
    project = make_server_project(api_source=mcp_app_source("auth"))
    token = jwt.encode({"sub": "7", "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    with project.start() as server:
        resp = server.client.post(
            server.url("/mcp"),
            content=rpc_body("initialize", INITIALIZE_PARAMS),
            headers=mcp_headers(authorization=f"Bearer {token}"),
        )
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id")

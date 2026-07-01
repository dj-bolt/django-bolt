"""Tier 1 auth: django-bolt JWT/guards enforced in Rust + per-tool guards."""

from __future__ import annotations

from _helpers import initialize, mint_jwt, parse_rpc, post_rpc
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI, HasPermission, IsAuthenticated, JWTAuthentication, Request
from django_bolt.testing import TestClient

SECRET = "tier1-secret-key-0123456789-abcdefgh"


def _build():
    api = BoltAPI()
    mcp = MCP("auth-server")

    @mcp.tool
    async def whoami(request: Request) -> dict:
        """Return the authenticated principal from the request context."""
        ctx = request.context or {}
        return {"user_id": ctx.get("user_id")}

    @mcp.tool(guards=[HasPermission("items:read")])
    async def admin_only() -> dict:
        return {"ok": True}

    mount_mcp(api, mcp, auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    return api, mcp


def test_missing_token_is_rejected():
    api, _ = _build()
    with TestClient(api) as client:
        resp, _ = initialize(client)  # no Authorization header
        assert resp.status_code == 401


def test_valid_token_exposes_principal_to_tool():
    api, _ = _build()
    bearer = f"Bearer {mint_jwt(SECRET, sub='42')}"
    with TestClient(api) as client:
        resp, session_id = initialize(client, authorization=bearer)
        assert resp.status_code == 200
        called = post_rpc(
            client,
            "tools/call",
            {"name": "whoami", "arguments": {}},
            session_id=session_id,
            authorization=bearer,
        )
        assert parse_rpc(called)["result"]["structuredContent"] == {"user_id": "42"}


def test_per_tool_guard_filters_tools_list():
    api, _ = _build()
    no_perm = f"Bearer {mint_jwt(SECRET, permissions=[])}"
    with_perm = f"Bearer {mint_jwt(SECRET, permissions=['items:read'])}"
    with TestClient(api) as client:
        _, sid_no = initialize(client, authorization=no_perm)
        listed_no = post_rpc(client, "tools/list", session_id=sid_no, authorization=no_perm)
        names_no = {t["name"] for t in parse_rpc(listed_no)["result"]["tools"]}
        assert "admin_only" not in names_no
        assert "whoami" in names_no

        _, sid_yes = initialize(client, authorization=with_perm)
        listed_yes = post_rpc(client, "tools/list", session_id=sid_yes, authorization=with_perm)
        names_yes = {t["name"] for t in parse_rpc(listed_yes)["result"]["tools"]}
        assert "admin_only" in names_yes


def test_per_tool_guard_allows_then_rejects_call():
    api, _ = _build()
    no_perm = f"Bearer {mint_jwt(SECRET, permissions=[])}"
    with_perm = f"Bearer {mint_jwt(SECRET, permissions=['items:read'])}"
    with TestClient(api) as client:
        # Authorized: the guarded tool runs and returns its result.
        _, sid_yes = initialize(client, authorization=with_perm)
        allowed = post_rpc(
            client,
            "tools/call",
            {"name": "admin_only", "arguments": {}},
            session_id=sid_yes,
            authorization=with_perm,
        )
        assert parse_rpc(allowed)["result"]["structuredContent"] == {"ok": True}

        # Unauthorized: the guarded tool must not execute successfully.
        _, sid_no = initialize(client, authorization=no_perm)
        rejected = post_rpc(
            client,
            "tools/call",
            {"name": "admin_only", "arguments": {}},
            session_id=sid_no,
            authorization=no_perm,
        )
        body = parse_rpc(rejected)
        succeeded = "error" not in body and body.get("result", {}).get("isError") is False
        assert not succeeded, "a tool whose guard fails must not execute successfully"

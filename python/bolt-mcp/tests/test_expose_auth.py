"""An exposed route keeps its own ``auth`` and guards over MCP (#928)."""

from __future__ import annotations

import pytest
from _helpers import initialize, mint_jwt, parse_rpc, post_rpc, post_rpc_modern
from bolt_mcp import MCP
from django.test import override_settings

from django_bolt import (
    APIKeyAuthentication,
    BoltAPI,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
)
from django_bolt.middleware import BaseMiddleware, rate_limit
from django_bolt.testing import TestClient

SECRET = "expose-auth-secret-key-0123456789-abcdef"


def _call(client, name: str, arguments: dict | None = None, *, authorization: str | None = None) -> dict:
    resp = post_rpc_modern(
        client, "tools/call", {"name": name, "arguments": arguments or {}}, authorization=authorization
    )
    assert resp.status_code == 200
    return parse_rpc(resp)["result"]


def _listed(client, *, authorization: str | None = None) -> set[str]:
    resp = post_rpc_modern(client, "tools/list", authorization=authorization)
    assert resp.status_code == 200
    return {tool["name"] for tool in parse_rpc(resp)["result"]["tools"]}


def _refused(result: dict) -> bool:
    return result["isError"] is True and "structuredContent" not in result


def _secret_api(*, mount_auth=None):
    """The example of #928: a JWT route with guards, exposed on a mount with no auth."""
    api = BoltAPI()
    calls: list[str] = []

    @api.get("/secret", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def secret() -> dict:
        """Read a secret."""
        calls.append("secret")
        return {"secret": 42}

    @api.get("/items", auth=[JWTAuthentication(secret=SECRET)], guards=[Requires("permissions", "items:read")])
    async def items() -> dict:
        """List the items."""
        calls.append("items")
        return {"items": []}

    @api.get("/public")
    async def public() -> dict:
        """Read public data."""
        calls.append("public")
        return {"public": True}

    api.mount_mcp(MCP("expose-auth"), expose=[secret, items, public], auth=mount_auth)
    return api, calls


def test_an_anonymous_call_to_a_guarded_route_is_refused():
    api, calls = _secret_api()
    with TestClient(api) as client:
        assert client.get("/secret").status_code == 401
        assert _refused(_call(client, "secret"))
    assert calls == []


def test_a_call_with_a_valid_token_runs_the_route():
    api, calls = _secret_api()
    bearer = f"Bearer {mint_jwt(SECRET, sub='7')}"
    with TestClient(api) as client:
        assert _call(client, "secret", authorization=bearer)["structuredContent"] == {"secret": 42}
    assert calls == ["secret"]


def test_a_token_without_the_permission_is_refused():
    api, calls = _secret_api()
    no_perm = f"Bearer {mint_jwt(SECRET, sub='7', permissions=[])}"
    with_perm = f"Bearer {mint_jwt(SECRET, sub='7', permissions=['items:read'])}"
    with TestClient(api) as client:
        assert client.get("/items", headers={"Authorization": no_perm}).status_code == 403
        assert _refused(_call(client, "items", authorization=no_perm))
        assert calls == []
        assert _call(client, "items", authorization=with_perm)["structuredContent"] == {"items": []}
    assert calls == ["items"]


def test_a_token_that_only_the_mount_accepts_is_refused():
    """The auth of the mount does not replace the auth of the route."""
    api, calls = _secret_api(mount_auth=[APIKeyAuthentication(api_keys={"mount-key"})])
    with TestClient(api) as client:
        resp = post_rpc_modern(
            client, "tools/call", {"name": "secret", "arguments": {}}, headers_override={"X-API-Key": "mount-key"}
        )
        assert _refused(parse_rpc(resp)["result"])
    assert calls == []


def test_tool_middleware_gets_the_principal_of_the_route():
    api = BoltAPI()
    mcp = MCP("principal")
    principals: list[dict] = []

    @api.get("/mine", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def mine() -> dict:
        return {}

    @mcp.tool_middleware
    async def record(call, call_next):
        principals.append(call.principal)
        return await call_next()

    api.mount_mcp(mcp, expose=[mine])
    with TestClient(api) as client:
        _call(client, "mine", authorization=f"Bearer {mint_jwt(SECRET, sub='7')}")
    assert [principal["user_id"] for principal in principals] == ["7"]


def test_a_route_with_no_auth_is_unchanged():
    api, calls = _secret_api()
    with TestClient(api) as client:
        assert _call(client, "public")["structuredContent"] == {"public": True}
    assert calls == ["public"]


def test_tools_list_hides_a_route_that_the_caller_cannot_call():
    api, _ = _secret_api()
    bearer = f"Bearer {mint_jwt(SECRET, sub='7', permissions=['items:read'])}"
    with TestClient(api) as client:
        assert _listed(client) == {"public"}
        assert _listed(client, authorization=bearer) == {"public", "secret", "items"}


def test_a_legacy_session_checks_the_credential_of_each_call():
    """The session does not keep the principal of ``initialize``: each call has its own headers."""
    api, calls = _secret_api()
    bearer = f"Bearer {mint_jwt(SECRET, sub='7')}"
    with TestClient(api) as client:
        _, session_id = initialize(client, authorization=bearer)
        params = {"name": "secret", "arguments": {}}
        refused = post_rpc(client, "tools/call", params, session_id=session_id)
        assert _refused(parse_rpc(refused)["result"])
        allowed = post_rpc(client, "tools/call", params, session_id=session_id, authorization=bearer)
        assert parse_rpc(allowed)["result"]["structuredContent"] == {"secret": 42}
    assert calls == ["secret"]


def test_default_guards_from_settings_apply_to_an_exposed_route(settings):
    with override_settings(BOLT_DEFAULT_PERMISSION_CLASSES=[IsAuthenticated()]):
        api = BoltAPI()

        @api.get("/me")
        async def me() -> dict:
            return {"me": True}

        api.mount_mcp(MCP("default-guards"), expose=[me], guards=[])
        bearer = f"Bearer {mint_jwt(settings.SECRET_KEY, sub='1')}"
        with TestClient(api) as client:
            assert client.get("/me").status_code == 401
            assert _refused(_call(client, "me"))
            assert _call(client, "me", authorization=bearer)["structuredContent"] == {"me": True}


def test_a_cookie_token_from_a_cross_site_page_is_refused():
    """The cookie CSRF check of the route also runs for a tool call."""
    api = BoltAPI()

    @api.post("/act", auth=[JWTAuthentication(secret=SECRET, cookie="access_token")], guards=[IsAuthenticated()])
    async def act() -> dict:
        return {"done": True}

    api.mount_mcp(MCP("cookie"), expose=[act])
    cookie = f"access_token={mint_jwt(SECRET, sub='7')}"
    params = {"name": "act", "arguments": {}}
    with TestClient(api) as client:
        cross_site = post_rpc_modern(
            client, "tools/call", params, headers_override={"Cookie": cookie, "Origin": "https://evil.example"}
        )
        assert _refused(parse_rpc(cross_site)["result"])
        same_site = post_rpc_modern(
            client, "tools/call", params, headers_override={"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}
        )
        assert parse_rpc(same_site)["result"]["structuredContent"] == {"done": True}


class _Tag(BaseMiddleware):
    async def process_request(self, request):
        return await self.get_response(request)


def test_a_route_with_a_rate_limit_cannot_be_exposed():
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1)
    async def limited() -> dict:
        return {}

    with pytest.raises(ValueError, match=r"limited.*rate_limit"):
        api.mount_mcp(MCP("limited"), expose=[limited])


def test_a_route_with_django_middleware_cannot_be_exposed():
    api = BoltAPI(django_middleware=["django.middleware.security.SecurityMiddleware"])

    @api.get("/secure")
    async def secure() -> dict:
        return {}

    with pytest.raises(ValueError, match=r"secure.*SecurityMiddleware"):
        api.mount_mcp(MCP("secure"), expose=[secure])


def test_a_route_with_python_middleware_cannot_be_exposed():
    api = BoltAPI(middleware=[_Tag])

    @api.get("/tagged")
    async def tagged() -> dict:
        return {}

    with pytest.raises(ValueError, match=r"tagged.*_Tag"):
        api.mount_mcp(MCP("tagged"), expose=[tagged])

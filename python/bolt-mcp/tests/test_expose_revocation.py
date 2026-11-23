"""An exposed route checks token revocation on each MCP tool call, as over HTTP (#951)."""

from __future__ import annotations

import asyncio
import logging

from _helpers import mint_jwt, parse_rpc, post_rpc_modern
from bolt_mcp import MCP
from django.core.cache import caches

from django_bolt import APIKeyAuthentication, BoltAPI, IsAuthenticated, JWTAuthentication
from django_bolt.auth.revocation import DjangoCacheRevocation, InMemoryRevocation
from django_bolt.testing import TestClient

SECRET = "expose-revocation-secret-0123456789-abcdef"


def _call(client, name: str, *, authorization: str | None = None) -> dict:
    resp = post_rpc_modern(client, "tools/call", {"name": name, "arguments": {}}, authorization=authorization)
    assert resp.status_code == 200
    return parse_rpc(resp)["result"]


def _listed(client, *, authorization: str | None = None) -> set[str]:
    resp = post_rpc_modern(client, "tools/list", authorization=authorization)
    assert resp.status_code == 200
    return {tool["name"] for tool in parse_rpc(resp)["result"]["tools"]}


def _denied(result: dict, name: str = "secret") -> bool:
    return (
        result["isError"] is True
        and "structuredContent" not in result
        and result["content"][0]["text"] == f"Permission denied for tool '{name}'"
    )


def _bearer(jti: str, **extra) -> str:
    return f"Bearer {mint_jwt(SECRET, sub='7', extra={'jti': jti, **extra})}"


def _api(auth: list, tool_middleware=None):
    api = BoltAPI()
    calls: list[str] = []

    @api.get("/secret", auth=auth, guards=[IsAuthenticated()])
    async def secret() -> dict:
        """Read a secret."""
        calls.append("secret")
        return {"secret": 42}

    @api.get("/open", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    async def open_route() -> dict:
        """Read data with no revocation check."""
        calls.append("open")
        return {"open": True}

    mcp = MCP("expose-revocation")
    if tool_middleware is not None:
        mcp.tool_middleware(tool_middleware)
    api.mount_mcp(mcp, expose=[secret, open_route])
    return api, calls


def test_a_revoked_jti_is_refused_over_http_and_mcp():
    store = InMemoryRevocation()
    api, calls = _api([JWTAuthentication(secret=SECRET, revocation_store=store)])
    asyncio.run(store.revoke("j1"))
    bearer = _bearer("j1")
    with TestClient(api) as client:
        assert client.get("/secret", headers={"Authorization": bearer}).status_code == 401
        assert _denied(_call(client, "secret", authorization=bearer))
    assert calls == []


def test_a_token_that_is_not_revoked_runs_the_route():
    store = InMemoryRevocation()
    api, calls = _api([JWTAuthentication(secret=SECRET, revocation_store=store)])
    asyncio.run(store.revoke("other"))
    bearer = _bearer("j2")
    with TestClient(api) as client:
        assert client.get("/secret", headers={"Authorization": bearer}).status_code == 200
        result = _call(client, "secret", authorization=bearer)
        assert result["isError"] is False
        assert result["structuredContent"] == {"secret": 42}
    assert calls == ["secret", "secret"]


def test_a_route_with_no_revocation_check_is_not_affected():
    api, calls = _api([JWTAuthentication(secret=SECRET, revocation_store=InMemoryRevocation())])
    bearer = _bearer("j3")
    with TestClient(api) as client:
        assert _call(client, "open_route", authorization=bearer)["structuredContent"] == {"open": True}
    assert calls == ["open"]


def test_a_revoked_jti_in_the_django_cache_store_is_refused():
    store = DjangoCacheRevocation()
    api, calls = _api([JWTAuthentication(secret=SECRET, revocation_store=store)])
    asyncio.run(store.revoke("j4"))
    bearer = _bearer("j4")
    try:
        with TestClient(api) as client:
            assert client.get("/secret", headers={"Authorization": bearer}).status_code == 401
            assert _denied(_call(client, "secret", authorization=bearer))
    finally:
        caches["default"].clear()
    assert calls == []


def test_a_reserved_jti_is_refused_as_revoked():
    """The store cannot check a jti with a reserved prefix, so the token counts as revoked."""
    api, calls = _api([JWTAuthentication(secret=SECRET, revocation_store=DjangoCacheRevocation())])
    bearer = _bearer("fam:abc")
    with TestClient(api) as client:
        assert client.get("/secret", headers={"Authorization": bearer}).status_code == 401
        assert _denied(_call(client, "secret", authorization=bearer))
    assert calls == []


def _family_and_version_api():
    """A handler that also checks the family and the user version of the token, from its claims."""
    store = InMemoryRevocation()

    async def revoked(jti: str, claims: dict) -> bool:
        return (
            await store.is_revoked(jti)
            or await store.is_family_revoked(claims["fam"])
            or claims["ver"] != await store.get_user_version(claims["sub"])
        )

    api, calls = _api([JWTAuthentication(secret=SECRET, revoked_token_handler=revoked)])
    return api, calls, store


def test_a_revoked_family_is_refused():
    api, calls, store = _family_and_version_api()
    asyncio.run(store.revoke_family("f1"))
    revoked = _bearer("j5", fam="f1", ver=0)
    valid = _bearer("j6", fam="f2", ver=0)
    with TestClient(api) as client:
        assert client.get("/secret", headers={"Authorization": revoked}).status_code == 401
        assert _denied(_call(client, "secret", authorization=revoked))
        assert _call(client, "secret", authorization=valid)["structuredContent"] == {"secret": 42}
    assert calls == ["secret"]


def test_a_stale_user_version_is_refused():
    api, calls, store = _family_and_version_api()
    asyncio.run(store.bump_user_version("7"))
    stale = _bearer("j7", fam="f3", ver=0)
    current = _bearer("j8", fam="f3", ver=1)
    with TestClient(api) as client:
        assert client.get("/secret", headers={"Authorization": stale}).status_code == 401
        assert _denied(_call(client, "secret", authorization=stale))
        assert _call(client, "secret", authorization=current)["structuredContent"] == {"secret": 42}
    assert calls == ["secret"]


def test_a_store_error_does_not_run_the_handler(caplog):
    """Over HTTP a store error gives 500. Over MCP it gives an error result, and the handler does not run."""

    async def broken(jti: str) -> bool:
        raise RuntimeError("store is down")

    api, calls = _api([JWTAuthentication(secret=SECRET, revoked_token_handler=broken)])
    bearer = _bearer("j9")
    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.get("/secret", headers={"Authorization": bearer}).status_code == 500
        with caplog.at_level(logging.ERROR, logger="bolt_mcp._execute"):
            result = _call(client, "secret", authorization=bearer)
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert any("store is down" in str(record.exc_info) for record in caplog.records)
    assert calls == []


def test_a_revoked_call_does_not_reach_tool_middleware():
    """The route refuses the token before tool middleware runs, as the other gate refusals do."""
    store = InMemoryRevocation()
    seen: list[str] = []

    async def middleware(call, call_next):
        seen.append(call.name)
        return await call_next()

    api, calls = _api([JWTAuthentication(secret=SECRET, revocation_store=store)], tool_middleware=middleware)
    asyncio.run(store.revoke("j10"))
    with TestClient(api) as client:
        assert _denied(_call(client, "secret", authorization=_bearer("j10")))
    assert seen == []
    assert calls == []


def test_a_backend_with_no_revocation_on_the_same_route_is_allowed():
    """An API key authenticates through a backend with no revocation. HTTP lets it through, so MCP does too."""
    store = InMemoryRevocation()
    api, calls = _api(
        [
            JWTAuthentication(secret=SECRET, revocation_store=store),
            APIKeyAuthentication(api_keys={"k1"}),
        ]
    )
    with TestClient(api) as client:
        assert client.get("/secret", headers={"X-API-Key": "k1"}).status_code == 200
        resp = post_rpc_modern(
            client, "tools/call", {"name": "secret", "arguments": {}}, headers_override={"X-API-Key": "k1"}
        )
        assert parse_rpc(resp)["result"]["structuredContent"] == {"secret": 42}
    assert calls == ["secret", "secret"]


def test_tools_list_does_not_check_revocation():
    """tools/list runs no store lookup, so it still lists the tool to a revoked token. A call is refused."""
    store = InMemoryRevocation()
    api, _calls = _api([JWTAuthentication(secret=SECRET, revocation_store=store)])
    asyncio.run(store.revoke("j11"))
    bearer = _bearer("j11")
    with TestClient(api) as client:
        assert "secret" in _listed(client, authorization=bearer)
        assert _denied(_call(client, "secret", authorization=bearer))

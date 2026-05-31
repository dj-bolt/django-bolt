"""Tier 2 auth: OAuth 2.1 Resource Server (RFC 9728 metadata + 401 challenge)."""

from __future__ import annotations

import jwt
from _helpers import initialize, mint_jwt
from bolt_mcp import MCP, ProtectedResource, mount_mcp

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

SECRET = "tier2-secret-key-0123456789-abcdefgh"
RESOURCE_URL = "https://api.test/mcp"
WELL_KNOWN = "/.well-known/oauth-protected-resource"


def _verify(token: str):
    try:
        return jwt.decode(token, SECRET, algorithms=["HS256"], audience=RESOURCE_URL)
    except jwt.PyJWTError:
        return None


def _build():
    api = BoltAPI()
    mcp = MCP("oauth-server")

    @mcp.tool
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    mount_mcp(
        api,
        mcp,
        oauth=ProtectedResource(
            resource_url=RESOURCE_URL,
            authorization_servers=["https://idp.test"],
            token_verifier=_verify,
        ),
    )
    return api, mcp


def test_protected_resource_metadata_document():
    api, _ = _build()
    with TestClient(api) as client:
        resp = client.get(WELL_KNOWN)
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["resource"] == RESOURCE_URL
        assert doc["authorization_servers"] == ["https://idp.test"]


def test_missing_token_challenges_with_www_authenticate():
    api, _ = _build()
    with TestClient(api) as client:
        resp, _ = initialize(client)  # no Authorization header
        assert resp.status_code == 401
        challenge = resp.headers.get("www-authenticate", "")
        assert "Bearer" in challenge
        assert "resource_metadata=" in challenge


def test_valid_token_allows_initialize():
    api, _ = _build()
    bearer = f"Bearer {mint_jwt(SECRET, audience=RESOURCE_URL)}"
    with TestClient(api) as client:
        resp, session_id = initialize(client, authorization=bearer)
        assert resp.status_code == 200
        assert session_id

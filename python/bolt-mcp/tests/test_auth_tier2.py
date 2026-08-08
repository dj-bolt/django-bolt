"""Tier 2 auth: OAuth 2.1 Resource Server (RFC 9728 metadata + 401 challenge)."""

from __future__ import annotations

import jwt
from _helpers import initialize, mint_jwt
from bolt_mcp import MCP, ProtectedResource, mount_mcp
from bolt_mcp._dispatch import make_verify_token

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

SECRET = "tier2-secret-key-0123456789-abcdefgh"
RESOURCE_URL = "https://api.test/mcp"
# RFC 9728 location for a resource with path /mcp: the well-known segment is
# inserted between the origin and the resource path.
WELL_KNOWN = "/.well-known/oauth-protected-resource/mcp"


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
        # The challenge points at the RFC 9728 path-aware location.
        assert 'resource_metadata="https://api.test/.well-known/oauth-protected-resource/mcp"' in challenge


def test_valid_token_allows_initialize():
    api, _ = _build()
    bearer = f"Bearer {mint_jwt(SECRET, audience=RESOURCE_URL)}"
    with TestClient(api) as client:
        resp, session_id = initialize(client, authorization=bearer)
        assert resp.status_code == 200
        assert session_id


def test_bearer_scheme_is_case_insensitive():
    api, _ = _build()
    bearer = f"bEaReR {mint_jwt(SECRET, audience=RESOURCE_URL)}"
    with TestClient(api) as client:
        resp, session_id = initialize(client, authorization=bearer)
        assert resp.status_code == 200
        assert session_id


def test_scope_claim_accepts_string_and_json_arrays():
    resource = ProtectedResource(
        resource_url=RESOURCE_URL,
        required_scopes=("read", "write"),
        token_verifier=lambda token: {"sub": token, "scope": ["read", "write"]},
    )
    principal = make_verify_token(resource)("user-1")
    assert principal is not None
    assert principal["user_id"] == "user-1"

    resource.token_verifier = lambda _token: {"sub": "user-2", "scp": "read write"}
    assert make_verify_token(resource)("token") is not None


def test_malformed_scope_claim_is_rejected_without_raising():
    resource = ProtectedResource(
        resource_url=RESOURCE_URL,
        required_scopes=("read",),
        token_verifier=lambda _token: {"scope": {"read": True}},
    )
    assert make_verify_token(resource)("token") is None


def test_claim_encoding_failure_is_logged_and_keeps_empty_claims_json(monkeypatch, caplog):
    resource = ProtectedResource(
        resource_url=RESOURCE_URL,
        token_verifier=lambda _token: {"sub": "user-1"},
    )

    def fail_encode(_claims):
        raise TypeError("not serializable")

    monkeypatch.setattr("bolt_mcp._dispatch.json_encode", fail_encode)
    principal = make_verify_token(resource)("token")
    assert principal is not None
    assert principal["claims_json"] is None
    assert "Failed to encode OAuth token claims" in caplog.text

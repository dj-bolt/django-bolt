"""Tier 2 auth: OAuth 2.1 Resource Server (RFC 9728 metadata + 401 challenge)."""

from __future__ import annotations

import time

import jwt
import pytest
from _helpers import initialize, mint_jwt, parse_rpc, post_rpc
from bolt_mcp import MCP, ProtectedResource, mount_mcp, principal
from bolt_mcp._dispatch import make_verify_token

from django_bolt import BoltAPI, Request, Requires
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


def test_more_than_one_space_ends_the_bearer_scheme():
    """RFC 9110, section 11.4: one or more spaces follow the scheme. A tab does not."""
    api, _ = _build()
    token = mint_jwt(SECRET, audience=RESOURCE_URL)
    with TestClient(api) as client:
        resp, session_id = initialize(client, authorization=f"Bearer   {token}")
        assert resp.status_code == 200
        assert session_id
        resp, _ = initialize(client, authorization=f"Bearer\t{token}")
        assert resp.status_code == 401


def _build_with_guards():
    api = BoltAPI()
    mcp = MCP("oauth-guards")

    @mcp.tool
    async def whoami(request: Request) -> dict:
        context = principal(request)
        return {key: context.get(key) for key in ("user_id", "is_staff", "is_superuser", "permissions")}

    @mcp.tool(guards=[Requires("is_staff", True)])
    async def staff_only() -> dict:
        return {"ok": True}

    @mcp.tool(guards=[Requires("permissions", none_of=["admin"])])
    async def not_admin() -> dict:
        return {"ok": True}

    resource = ProtectedResource(
        resource_url=RESOURCE_URL, authorization_servers=["https://idp.test"], token_verifier=_verify
    )
    mount_mcp(api, mcp, oauth=resource)
    return api


def _call(client, bearer: str, session_id: str, name: str) -> dict | None:
    """The structured result of the tool, or None when the call fails."""
    body = parse_rpc(post_rpc(client, "tools/call", {"name": name}, session_id=session_id, authorization=bearer))
    result = body.get("result", {})
    return None if "error" in body or result.get("isError") else result["structuredContent"]


@pytest.mark.parametrize(
    "claims",
    [
        {"is_staff": "false", "is_superuser": "no", "permissions": "admin"},
        {"is_staff": 1, "is_superuser": 1, "permissions": ["admin", 1]},
        {"is_staff": [True], "is_superuser": None, "permissions": None},
    ],
    ids=["strings", "numbers", "other"],
)
def test_a_verified_claim_grants_only_in_its_exact_form(claims):
    """``is_staff`` grants only for JSON ``true``, ``permissions`` only as an array of strings, as on a route."""
    bearer = f"Bearer {mint_jwt(SECRET, audience=RESOURCE_URL, extra=claims)}"
    with TestClient(_build_with_guards()) as client:
        resp, session_id = initialize(client, authorization=bearer)
        assert resp.status_code == 200
        assert _call(client, bearer, session_id, "whoami") == {
            "user_id": "1",
            "is_staff": False,
            "is_superuser": False,
            "permissions": None,
        }
        # Each guard reads a malformed claim, so each refuses, `none_of` too.
        assert _call(client, bearer, session_id, "staff_only") is None
        assert _call(client, bearer, session_id, "not_admin") is None


def test_a_verified_claim_of_the_exact_form_grants():
    claims = {"is_staff": True, "is_superuser": True, "permissions": ["read"]}
    bearer = f"Bearer {mint_jwt(SECRET, audience=RESOURCE_URL, extra=claims)}"
    with TestClient(_build_with_guards()) as client:
        _, session_id = initialize(client, authorization=bearer)
        assert _call(client, bearer, session_id, "whoami") == {
            "user_id": "1",
            "is_staff": True,
            "is_superuser": True,
            "permissions": ["read"],
        }
        assert _call(client, bearer, session_id, "staff_only") == {"ok": True}
        assert _call(client, bearer, session_id, "not_admin") == {"ok": True}


def test_a_verified_time_claim_can_have_a_fraction():
    """RFC 7519, section 2: a NumericDate can have a fraction. The bridge refused such claims with a 500."""
    now = time.time()
    bearer = f"Bearer {mint_jwt(SECRET, audience=RESOURCE_URL, extra={'exp': now + 3600.5, 'iat': now - 0.5})}"
    with TestClient(_build_with_guards()) as client:
        resp, session_id = initialize(client, authorization=bearer)
        assert resp.status_code == 200
        assert _call(client, bearer, session_id, "whoami")["user_id"] == "1"


@pytest.mark.parametrize("sub", [1, True, ["1"], {"id": "1"}])
def test_a_verified_sub_that_is_not_a_string_is_refused(sub):
    """A route refuses a token whose ``sub`` is not a string. The bridge made such a ``sub`` a user id."""
    resource = ProtectedResource(resource_url=RESOURCE_URL, token_verifier=lambda _token: {"sub": sub})
    assert make_verify_token(resource)("token") is None


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

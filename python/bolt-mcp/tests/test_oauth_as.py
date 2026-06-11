"""Built-in OAuth 2.1 Authorization Server (bolt_mcp.oauth.AuthorizationServer).

Exercises the full MCP OAuth handshake against the in-process server: discovery
metadata, Dynamic Client Registration, Authorization Code + PKCE, refresh-token
rotation with reuse detection, hashed-at-rest secrets, CSRF/open-redirect guards, and
that the issued JWT authenticates to /mcp and drives per-tool guards.
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlsplit

import jwt
from _helpers import initialize, parse_rpc, post_rpc
from bolt_mcp import MCP, AuthorizationServer, mount_mcp
from bolt_mcp.oauth import sessions as oauth_sessions
from bolt_mcp.oauth.models import RefreshToken
from bolt_mcp.oauth.pkce import compute_s256, verify_s256
from bolt_mcp.oauth.tokens import sha256_hex
from django.conf import settings
from django.contrib.auth import get_user_model

from django_bolt import BoltAPI, HasPermission
from django_bolt.testing import TestClient

ISSUER = "http://localhost:8000"
REDIRECT_URI = "http://localhost:9876/callback"
PASSWORD = "s3cr3t-pw-123456"


class _DemoAuth(AuthorizationServer):
    issuer = ISSUER
    scopes_supported = ("mcp",)

    def get_extra_claims(self, user, *, scopes, client_id):
        return {"permissions": ["reports:read"] if user.is_staff else []}


def _build(**server_kwargs):
    api = BoltAPI()
    mcp = MCP("oauth-as-server")

    @mcp.tool
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    @mcp.tool(guards=[HasPermission("reports:read")])
    async def secret() -> dict:
        return {"ok": True}

    server = _DemoAuth(**server_kwargs)
    mount_mcp(api, mcp, oauth=server)
    return api, mcp, server


def _make_user(username="alice", *, is_staff=False):
    User = get_user_model()
    User.objects.filter(username=username).delete()
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=is_staff)


def _register_client(client, redirect_uris=None):
    r = client.post(
        "/oauth/register", json={"redirect_uris": redirect_uris or [REDIRECT_URI], "client_name": "Test App"}
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _pkce():
    verifier = secrets.token_urlsafe(32)
    return verifier, compute_s256(verifier)


def _authorize_params(client_id, challenge, *, method="S256", redirect_uri=REDIRECT_URI, scope="mcp", state="st-1"):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": method,
    }


def _session_cookie(username="alice"):
    return {"Cookie": f"sessionid={oauth_sessions._login(username, PASSWORD)['session_key']}"}


def _get_code(client, params, username="alice"):
    """Approve consent via the Origin-checked POST with an existing Django session → code.

    A code is never issued from a bare GET (CSRF defense), so obtaining one means POSTing
    the explicit ``decision=approve`` with the session cookie and a same-origin ``Origin``.
    """
    r = client.post(
        "/oauth/authorize",
        data={**params, "decision": "approve"},
        headers={**_session_cookie(username), "Origin": ISSUER},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)


def _obtain_tokens(client, *, username="alice", is_staff=False, scope="mcp"):
    _make_user(username, is_staff=is_staff)
    client_id = _register_client(client)
    verifier, challenge = _pkce()
    q = _get_code(client, _authorize_params(client_id, challenge, scope=scope), username=username)
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    return tok.json()


# ── discovery ───────────────────────────────────────────────────────────────────
def test_authorization_server_metadata():
    api, _, _ = _build()
    with TestClient(api) as client:
        md = client.get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == ISSUER
    assert md["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert md["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert md["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert set(md["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert md["response_types_supported"] == ["code"]


def test_protected_resource_metadata_points_at_builtin_issuer():
    api, _, _ = _build()
    with TestClient(api) as client:
        md = client.get("/.well-known/oauth-protected-resource").json()
    assert md["authorization_servers"] == [ISSUER]
    assert md["resource"] == ISSUER


def test_missing_token_challenges_with_www_authenticate():
    api, _, _ = _build()
    with TestClient(api) as client:
        resp, _ = initialize(client)
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers.get("www-authenticate", "")


# ── dynamic client registration ──────────────────────────────────────────────────
def test_dynamic_client_registration():
    api, _, _ = _build()
    with TestClient(api) as client:
        r = client.post("/oauth/register", json={"redirect_uris": [REDIRECT_URI], "client_name": "Claude"})
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"]
    assert body["redirect_uris"] == [REDIRECT_URI]
    assert body["token_endpoint_auth_method"] == "none"


def test_registration_rejects_missing_redirect_uris():
    api, _, _ = _build()
    with TestClient(api) as client:
        r = client.post("/oauth/register", json={"client_name": "no-redirects"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


# ── authorize ─────────────────────────────────────────────────────────────────--
def test_authorize_without_session_shows_login():
    api, _, _ = _build()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        r = client.get("/oauth/authorize", params=_authorize_params(client_id, challenge), follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Sign in" in r.text


def test_authorize_unregistered_redirect_uri_is_not_redirected():
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        params = _authorize_params(client_id, challenge, redirect_uri="http://evil.test/cb")
        r = client.get("/oauth/authorize", params=params, headers=_session_cookie(), follow_redirects=False)
    # Open-redirect guard: a bad redirect_uri yields an inline error, never a 3xx redirect.
    assert r.status_code == 400
    assert "redirect_uri" in r.text


def test_verify_s256_rejects_non_ascii_verifier():
    """A non-ASCII verifier is invalid per RFC 7636 — must return False, never raise."""
    _, challenge = _pkce()
    assert verify_s256("üñïcode-verifier-é", challenge) is False


def test_authorize_rejects_plain_pkce():
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        params = _authorize_params(client_id, challenge, method="plain")
        r = client.get("/oauth/authorize", params=params, headers=_session_cookie(), follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["invalid_request"]


def test_authorize_post_login_flow_issues_code():
    api, _, _ = _build(auto_consent=True)
    _make_user(is_staff=True)
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        form = {**_authorize_params(client_id, challenge), "username": "alice", "password": PASSWORD}
        r = client.post("/oauth/authorize", data=form, headers={"Origin": ISSUER}, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["code"] and q["state"] == ["st-1"]


def test_authorize_post_requires_origin():
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        form = {**_authorize_params(client_id, challenge), "username": "alice", "password": PASSWORD}
        r = client.post("/oauth/authorize", data=form, follow_redirects=False)  # no Origin header
    assert r.status_code == 403


def test_authorize_post_bad_credentials_redisplays_login():
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        form = {**_authorize_params(client_id, challenge), "username": "alice", "password": "wrong"}
        r = client.post("/oauth/authorize", data=form, headers={"Origin": ISSUER}, follow_redirects=False)
    assert r.status_code == 200
    assert "Invalid username or password" in r.text


def test_authorize_get_never_issues_code_even_with_auto_consent():
    """Regression (CSRF): a bare GET carrying only an ambient session must not mint a code.

    Otherwise an attacker registers their own client + PKCE challenge and lures a signed-in
    victim to an /authorize GET URL; the AS would 302 a victim-scoped authorization code to
    the attacker's redirect_uri. With the fix, even auto_consent only renders the consent
    screen on GET — a code is issued solely through the Origin-checked approval POST.
    """
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        r = client.get(
            "/oauth/authorize",
            params=_authorize_params(client_id, challenge),
            headers=_session_cookie(),
            follow_redirects=False,
        )
    assert r.status_code == 200  # consent screen, not a 302 redirect carrying a code
    assert "location" not in {k.lower() for k in r.headers}
    assert "Allow" in r.text  # the consent approval button (distinguishes it from the login page)


# ── token endpoint ────────────────────────────────────────────────────────────--
def test_authorization_code_pkce_exchange():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        body = _obtain_tokens(client, is_staff=True)
    assert body["token_type"] == "Bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["scope"] == "mcp"


def test_token_rejects_wrong_code_verifier():
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        q = _get_code(client, _authorize_params(client_id, challenge))
        r = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"][0],
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": "the-wrong-verifier",
            },
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_authorization_code_is_single_use():
    api, _, _ = _build(auto_consent=True)
    _make_user()
    with TestClient(api) as client:
        client_id = _register_client(client)
        verifier, challenge = _pkce()
        q = _get_code(client, _authorize_params(client_id, challenge))
        data = {
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        first = client.post("/oauth/token", data=data)
        second = client.post("/oauth/token", data=data)
    assert first.status_code == 200
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"


def test_refresh_token_rotation_and_reuse_detection():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client, is_staff=True)
        old_refresh = tokens["refresh_token"]

        rotated = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": old_refresh})
        assert rotated.status_code == 200, rotated.text
        new_refresh = rotated.json()["refresh_token"]
        assert new_refresh and new_refresh != old_refresh

        # Replaying the rotated (old) token is theft → rejected, and revokes the chain.
        reuse = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": old_refresh})
        assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"

        revoked = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": new_refresh})
        assert revoked.status_code == 400


def test_secrets_are_stored_hashed():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client)
    refresh = tokens["refresh_token"]
    assert not RefreshToken.objects.filter(token_hash=refresh).exists()  # raw never stored
    assert RefreshToken.objects.filter(token_hash=sha256_hex(refresh)).exists()


def test_revoke_endpoint():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client)
        refresh = tokens["refresh_token"]
        assert client.post("/oauth/revoke", data={"token": refresh}).status_code == 200
        after = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh})
    assert after.status_code == 400


# ── /mcp with issued tokens ──────────────────────────────────────────────────────
def test_issued_token_authenticates_to_mcp():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client, is_staff=True)
        bearer = f"Bearer {tokens['access_token']}"
        resp, session_id = initialize(client, authorization=bearer)
    assert resp.status_code == 200
    assert session_id


def test_guarded_tool_visible_and_callable_with_permission():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client, username="staffer", is_staff=True)
        bearer = f"Bearer {tokens['access_token']}"
        _, sid = initialize(client, authorization=bearer)
        listed = post_rpc(client, "tools/list", {}, session_id=sid, authorization=bearer)
        names = {t["name"] for t in parse_rpc(listed)["result"]["tools"]}
        assert "secret" in names
        called = post_rpc(
            client, "tools/call", {"name": "secret", "arguments": {}}, session_id=sid, authorization=bearer
        )
    assert parse_rpc(called)["result"]["structuredContent"] == {"ok": True}


def test_guarded_tool_hidden_without_permission():
    api, _, _ = _build(auto_consent=True)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client, username="plain", is_staff=False)
        bearer = f"Bearer {tokens['access_token']}"
        _, sid = initialize(client, authorization=bearer)
        listed = post_rpc(client, "tools/list", {}, session_id=sid, authorization=bearer)
        names = {t["name"] for t in parse_rpc(listed)["result"]["tools"]}
    assert "secret" not in names
    assert "greet" in names


def test_required_scope_enforced_on_mcp():
    api, _, _ = _build(auto_consent=True, required_scopes=("admin",))
    with TestClient(api) as client:
        tokens = _obtain_tokens(client, is_staff=True, scope="mcp")  # token lacks "admin"
        bearer = f"Bearer {tokens['access_token']}"
        resp, _ = initialize(client, authorization=bearer)
    assert resp.status_code == 401


# ── Django-style overrides: subclass AuthorizationServer and override methods ──────
def _mount_subclass(server_cls):
    api = BoltAPI()
    mcp = MCP("override-server")

    @mcp.tool
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    mount_mcp(api, mcp, oauth=server_cls())
    return api


def test_subclass_overrides_get_extra_claims_with_scope_and_client():
    seen = {}

    class CustomAuth(AuthorizationServer):
        issuer = ISSUER
        auto_consent = True

        def get_extra_claims(self, user, *, scopes, client_id):
            seen["scopes"] = scopes
            seen["client_id"] = client_id
            return {"tenant_id": "acme", "permissions": ["reports:read"] if "mcp" in scopes else []}

    api = _mount_subclass(CustomAuth)
    with TestClient(api) as client:
        tokens = _obtain_tokens(client, is_staff=True, scope="mcp")
    claims = jwt.decode(
        tokens["access_token"], settings.SECRET_KEY, algorithms=["HS256"], audience=ISSUER, issuer=ISSUER
    )
    assert claims["tenant_id"] == "acme"
    assert claims["permissions"] == ["reports:read"]
    # The override received the OAuth grant context the dataclass callback couldn't.
    assert seen["scopes"] == ["mcp"]
    assert isinstance(seen["client_id"], str) and seen["client_id"]  # the registered client_id was passed


def test_subclass_overrides_render_login():
    class BrandedAuth(AuthorizationServer):
        issuer = ISSUER

        def render_login(self, params, *, error=None):
            return "<!doctype html><title>x</title><body>WELCOME TO ACME</body>"

    api = _mount_subclass(BrandedAuth)
    with TestClient(api) as client:
        client_id = _register_client(client)
        _, challenge = _pkce()
        r = client.get("/oauth/authorize", params=_authorize_params(client_id, challenge), follow_redirects=False)
    assert r.status_code == 200
    assert "WELCOME TO ACME" in r.text

"""Full built-in OAuth 2.1 Authorization Server flow over a real runbolt server.

Proves end to end, against a live process and real TCP, what the in-process TestClient
covers in unit tests: discovery → Dynamic Client Registration → Authorization Code + PKCE
(Django-session login) → token exchange → refresh-token rotation with reuse detection →
an authenticated /mcp call whose per-tool guard is driven by the issued token's claims.

``issuer`` is fixed to ``http://testserver`` (independent of the harness's random port):
the token ``aud``/``iss`` and the ``/authorize`` Origin check stay self-consistent, and we
drive every endpoint directly so the discovery URLs never need to be dialable.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from _helpers import parse_rpc, rpc_body
from bolt_mcp.oauth.pkce import compute_s256

pytestmark = pytest.mark.server_integration

DUAL_ACCEPT = "application/json, text/event-stream"
ISSUER = "http://testserver"
REDIRECT_URI = "http://localhost:9999/callback"
USERNAME = "alice"
PASSWORD = "s3cr3t-pw-123456"

MCP_API_BODY = f"""
from bolt_mcp import MCP, mount_mcp, principal
from bolt_mcp.oauth import AuthorizationServer
from django_bolt import HasPermission, IsAuthenticated, Request

mcp = MCP("oauth-itest", "1.0")


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {{"sum": a + b}}


@mcp.tool(guards=[IsAuthenticated()])
async def whoami(request: Request) -> dict:
    return principal(request)


@mcp.tool(guards=[HasPermission("reports:read")])
async def read_report() -> dict:
    return {{"report": "Q3 up 42%"}}


class ITestAuth(AuthorizationServer):
    issuer = "{ISSUER}"
    auto_consent = True  # the login POST issues the code directly

    def get_extra_claims(self, user, *, scopes, client_id):
        return {{"permissions": ["reports:read"] if user.is_staff else []}}


mount_mcp(api, mcp, oauth=ITestAuth())
"""

_CREATE_USER = (
    "from django.contrib.auth import get_user_model as M;"
    f"u = M().objects.create_user({USERNAME!r}, password={PASSWORD!r});"
    "u.is_staff = True; u.save()"
)


def _manage(project, *args: str) -> None:
    """Run a manage.py command in the generated project (same interpreter + PYTHONPATH)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(project.root), env.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, str(project.path("manage.py")), *args],
        cwd=str(project.root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"manage.py {args} failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")


def _register_client(server) -> str:
    reg = server.client.post(
        server.url("/oauth/register"), json={"redirect_uris": [REDIRECT_URI], "client_name": "itest"}
    )
    assert reg.status_code == 201, reg.text
    return reg.json()["client_id"]


def _login_for_tokens(server, client_id: str, *, scope: str = "mcp") -> dict:
    """Drive Authorization Code + PKCE end to end (auto_consent → the login POST issues the code)."""
    verifier = secrets.token_urlsafe(32)
    authz = server.client.post(
        server.url("/oauth/authorize"),
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": scope,
            "state": "xyz",
            "code_challenge": compute_s256(verifier),
            "code_challenge_method": "S256",
            "username": USERNAME,
            "password": PASSWORD,
        },
        headers={"Origin": ISSUER},
        follow_redirects=False,
    )
    assert authz.status_code == 302, authz.text
    code = parse_qs(urlsplit(authz.headers["location"]).query)["code"][0]
    tok = server.client.post(
        server.url("/oauth/token"),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    return tok.json()


def test_oauth_authorization_code_flow_end_to_end(make_server_project):
    project = make_server_project(
        project_api_body=MCP_API_BODY,
        installed_apps=["django.contrib.sessions", "bolt_mcp.oauth"],
    )
    _manage(project, "migrate", "--noinput")
    _manage(project, "shell", "-c", _CREATE_USER)

    with project.start(processes=1) as server:
        c = server.client
        init_body = rpc_body(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "it", "version": "1"}},
        )
        anon_headers = {"Accept": DUAL_ACCEPT, "Content-Type": "application/json"}

        # 1. Discovery metadata is served and advertises the built-in issuer + endpoints.
        md = c.get(server.url("/.well-known/oauth-authorization-server")).json()
        assert md["issuer"] == ISSUER
        assert md["registration_endpoint"].endswith("/oauth/register")
        assert md["code_challenge_methods_supported"] == ["S256"]
        prm = c.get(server.url("/.well-known/oauth-protected-resource")).json()
        assert prm["authorization_servers"] == [ISSUER]

        # 2. Unauthenticated /mcp is challenged with WWW-Authenticate (this is what makes
        #    an OAuth client start the flow).
        unauth = c.post(server.url("/mcp"), content=init_body, headers=anon_headers)
        assert unauth.status_code == 401
        assert "resource_metadata=" in unauth.headers.get("www-authenticate", "")

        # 3. Dynamic Client Registration.
        reg = c.post(server.url("/oauth/register"), json={"redirect_uris": [REDIRECT_URI], "client_name": "itest"})
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        # 4. Authorization Code + PKCE: log in with Django credentials; auto_consent issues
        #    the code on the (Origin-checked) login POST.
        verifier = secrets.token_urlsafe(32)
        challenge = compute_s256(verifier)
        authz = c.post(
            server.url("/oauth/authorize"),
            data={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": "mcp",
                "state": "xyz",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "username": USERNAME,
                "password": PASSWORD,
            },
            headers={"Origin": ISSUER},
            follow_redirects=False,
        )
        assert authz.status_code == 302, authz.text
        redirect_q = parse_qs(urlsplit(authz.headers["location"]).query)
        assert redirect_q["state"] == ["xyz"]
        code = redirect_q["code"][0]

        # 5. Exchange the code for tokens (PKCE verified).
        tok = c.post(
            server.url("/oauth/token"),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200, tok.text
        tokens = tok.json()
        access, refresh = tokens["access_token"], tokens["refresh_token"]
        assert tokens["token_type"] == "Bearer"

        # 6. Refresh rotates the token; replaying the old one is rejected (reuse → revoke).
        rotated = c.post(server.url("/oauth/token"), data={"grant_type": "refresh_token", "refresh_token": refresh})
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["refresh_token"] != refresh
        reused = c.post(server.url("/oauth/token"), data={"grant_type": "refresh_token", "refresh_token": refresh})
        assert reused.status_code == 400

        # 7. The issued access token authenticates to /mcp and drives the per-tool guards.
        bearer = {**anon_headers, "Authorization": f"Bearer {access}"}
        init = c.post(server.url("/mcp"), content=init_body, headers=bearer)
        assert init.status_code == 200, init.text
        session_id = init.headers["mcp-session-id"]

        sess = {**bearer, "Mcp-Session-Id": session_id}
        listed = c.post(server.url("/mcp"), content=rpc_body("tools/list", id=2), headers=sess)
        names = {t["name"] for t in parse_rpc(listed)["result"]["tools"]}
        assert {"add", "whoami", "read_report"} <= names  # staff token grants reports:read

        called = c.post(
            server.url("/mcp"),
            content=rpc_body("tools/call", {"name": "read_report", "arguments": {}}, id=3),
            headers=sess,
        )
        assert parse_rpc(called)["result"]["structuredContent"] == {"report": "Q3 up 42%"}


def test_oauth_client_and_refresh_token_survive_server_restart(make_server_project):
    """ORM-backed state must outlive the process that created it.

    Clients, codes, and refresh tokens are persisted in the database — not in process
    memory — precisely so a linked connector keeps working across a restart or a
    multi-process (SO_REUSEPORT) redeploy. TestClient cannot prove this: it needs a
    second, fresh process reading the same on-disk DB. We register + issue tokens on
    one server, stop it, then start a new server on the same project/DB and confirm both
    the dynamically registered client and the pre-restart refresh token are still honored.
    """
    project = make_server_project(
        project_api_body=MCP_API_BODY,
        installed_apps=["django.contrib.sessions", "bolt_mcp.oauth"],
    )
    _manage(project, "migrate", "--noinput")
    _manage(project, "shell", "-c", _CREATE_USER)

    with project.start(processes=1) as server:
        client_id = _register_client(server)
        refresh = _login_for_tokens(server, client_id)["refresh_token"]

    # New process, same on-disk database (the first server is fully stopped here).
    with project.start(processes=1) as server2:
        # The refresh token minted before the restart still rotates → it was persisted in
        # the DB, not held in the now-dead process's memory.
        rotated = server2.client.post(
            server2.url("/oauth/token"),
            data={"grant_type": "refresh_token", "refresh_token": refresh},
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["refresh_token"] != refresh

        # The dynamically registered client also survived: a brand-new code flow reusing
        # the same client_id (no re-registration) succeeds against the fresh process.
        assert _login_for_tokens(server2, client_id)["access_token"]

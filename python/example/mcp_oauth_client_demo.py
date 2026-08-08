"""Headless end-to-end demo: OAuth 2.1 handshake + MCP calls against ``/mcp``.

Walks the exact flow a real MCP client (Claude Code, MCP Inspector, ...) performs
against the example server in ``mcp_demo/api.py`` — no browser needed:

    1. discovery      401 challenge → resource_metadata URL → authorization server
    2. registration   POST /oauth/register (RFC 7591 dynamic client registration)
    3. authorize      POST /oauth/authorize with Django credentials + PKCE (S256)
    4. consent        POST /oauth/authorize decision=approve (session cookie)
    5. token          POST /oauth/token (authorization_code + code_verifier)
    6. MCP            initialize / tools/list / tools/call with the Bearer token

Run the server first (issuer in mcp_demo/api.py is pinned to port 8001):

    python manage.py migrate
    python manage.py runbolt --host 127.0.0.1 --port 8001 --processes 1

then:

    python mcp_oauth_client_demo.py [--username demo] [--password demo12345]

The script prints the access token at the end so you can hand it to another MCP
client, e.g. Claude Code:

    claude mcp add --transport http bolt-example http://127.0.0.1:8001/mcp \
        --header "Authorization: Bearer <token>"

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

REDIRECT_URI = "http://127.0.0.1:8765/callback"  # never actually served; we read the 302


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None  # surface 3xx responses instead of following them


OPENER = urllib.request.build_opener(NoRedirect)


def http(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        return OPENER.open(req)
    except urllib.error.HTTPError as e:
        return e  # HTTPError is also a response object; let callers inspect status/body


def get_json(url: str) -> dict:
    resp = http("GET", url)
    body = resp.read()
    if resp.status != 200:
        sys.exit(f"GET {url} failed: {resp.status} {body[:500]!r}")
    return json.loads(body)


def post_form(url: str, fields: dict, *, headers: dict | None = None):
    data = urllib.parse.urlencode(fields).encode()
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return http("POST", url, data=data, headers=hdrs)


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8001", help="server origin (must equal the issuer)")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default="demo12345")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    # ── 1. discovery ────────────────────────────────────────────────────────────
    step(1, "discovery: 401 challenge → protected-resource → authorization-server metadata")
    # The real client flow: an unauthenticated request to /mcp answers 401
    # with WWW-Authenticate carrying the resource_metadata URL (RFC 9728).
    challenge = http("POST", f"{base}/mcp", data=b"{}", headers={"Content-Type": "application/json"})
    www_authenticate = challenge.headers.get("WWW-Authenticate", "")
    marker = 'resource_metadata="'
    if marker not in www_authenticate:
        sys.exit(f"expected a resource_metadata challenge, got: {www_authenticate!r}")
    metadata_url = www_authenticate.split(marker, 1)[1].split('"', 1)[0]
    pr = get_json(metadata_url)
    issuer = pr["authorization_servers"][0]
    asm = get_json(f"{issuer}/.well-known/oauth-authorization-server")
    print(f"    resource={pr['resource']}  issuer={issuer}")
    print(f"    authorize={asm['authorization_endpoint']}  token={asm['token_endpoint']}")

    # ── 2. dynamic client registration ─────────────────────────────────────────
    step(2, "dynamic client registration (RFC 7591)")
    reg = http(
        "POST",
        asm["registration_endpoint"],
        data=json.dumps(
            {
                "client_name": "mcp-oauth-client-demo",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "application_type": "native",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    reg_body = json.loads(reg.read())
    if reg.status != 201:
        sys.exit(f"registration failed: {reg.status} {reg_body}")
    client_id = reg_body["client_id"]
    print(f"    client_id={client_id}")

    # ── 3. authorize: sign in with Django credentials + PKCE ───────────────────
    step(3, f"authorize: sign in as {args.username!r} (PKCE S256)")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    oauth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    login = post_form(
        asm["authorization_endpoint"],
        {**oauth_params, "username": args.username, "password": args.password},
        headers={"Origin": issuer},  # POST /authorize is Origin-checked (CSRF guard)
    )
    login_body = login.read()

    if login.status == 302:  # auto_consent server: code comes straight back
        location = login.headers["Location"]
    elif login.status == 200 and b"Invalid username or password" in login_body:
        sys.exit("login failed: invalid username or password (create the user first — see README)")
    elif login.status == 200:
        # ── 4. consent screen: approve with the session cookie ────────────────
        step(4, "consent: approve")
        cookie = login.headers.get("Set-Cookie", "").split(";", 1)[0]
        if not cookie:
            sys.exit(f"expected a session cookie on the consent page, got: {dict(login.headers)}")
        consent = post_form(
            asm["authorization_endpoint"],
            {**oauth_params, "decision": "approve"},
            headers={"Origin": issuer, "Cookie": cookie},
        )
        if consent.status != 302:
            sys.exit(f"consent failed: {consent.status} {consent.read()[:500]!r}")
        location = consent.headers["Location"]
    else:
        sys.exit(f"authorize failed: {login.status} {login_body[:500]!r}")

    redirect_query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
    code = redirect_query["code"]
    assert redirect_query["state"] == oauth_params["state"], "state mismatch"
    assert redirect_query["iss"] == issuer, "issuer mismatch (RFC 9207)"
    print(f"    authorization code: {code[:12]}…")

    # ── 5. token exchange ──────────────────────────────────────────────────────
    step(5, "token exchange (authorization_code + PKCE verifier)")
    tok = post_form(
        asm["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    tok_body = json.loads(tok.read())
    if tok.status != 200:
        sys.exit(f"token exchange failed: {tok.status} {tok_body}")
    token = tok_body["access_token"]
    print(f"    access_token: {token[:24]}…  (expires_in={tok_body['expires_in']}s)")

    # ── 6. speak MCP with the Bearer token ─────────────────────────────────────
    step(6, "MCP: initialize / tools/list / tools/call")
    mcp_url = pr["resource"]
    mcp_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def rpc(method: str, params: dict, *, id: int, session: str | None = None):
        hdrs = dict(mcp_headers)
        if session:
            hdrs["Mcp-Session-Id"] = session
        resp = http(
            "POST",
            mcp_url,
            data=json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).encode(),
            headers=hdrs,
        )
        raw = resp.read()
        if resp.status != 200:
            sys.exit(f"{method} failed: {resp.status} {raw[:500]!r}")
        if resp.headers.get_content_type() == "text/event-stream":
            frames = [ln[5:].strip() for ln in raw.decode().splitlines() if ln.startswith("data:")]
            payload = next(json.loads(f) for f in frames if f and f != "{}")
        else:
            payload = json.loads(raw)
        if "error" in payload:
            sys.exit(f"{method} error: {payload['error']}")
        return payload["result"], resp.headers.get("mcp-session-id")

    init, session = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-oauth-client-demo", "version": "1.0"},
        },
        id=1,
    )
    print(f"    server: {init['serverInfo']['name']} {init['serverInfo']['version']}  session={session}")

    tools, _ = rpc("tools/list", {}, id=2, session=session)
    names = sorted(t["name"] for t in tools["tools"])
    print(f"    tools ({len(names)}): {', '.join(names)}")

    result, _ = rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, id=3, session=session)
    print(f"    add(2, 3) → {result['structuredContent']}")

    result, _ = rpc("tools/call", {"name": "whoami", "arguments": {}}, id=4, session=session)
    print(f"    whoami() → {result['structuredContent']}")

    print("\nDone. Reuse the token with any MCP client, e.g.:\n")
    print(f"  claude mcp add --transport http bolt-example {mcp_url} \\")
    print(f'      --header "Authorization: Bearer {token}"')


if __name__ == "__main__":
    main()

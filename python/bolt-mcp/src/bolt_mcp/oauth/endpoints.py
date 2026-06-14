"""OAuth Authorization Server HTTP endpoints, registered on a ``BoltAPI``.

Routes (all public — ``auth=None``):
  GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
  POST {prefix}/register                         RFC 7591 Dynamic Client Registration
  GET  {prefix}/authorize                         login / consent (Authorization Code)
  POST {prefix}/authorize                         login submit + consent decision
  POST {prefix}/token                             code + refresh grants (PKCE)
  POST {prefix}/revoke                            RFC 7009 token revocation
"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode

import msgspec
from asgiref.sync import sync_to_async

from django_bolt import BoltAPI, Request
from django_bolt._json import decode as json_decode
from django_bolt.responses import HTML, JSON, Redirect

from . import consent, metadata, pkce, sessions, store, tokens
from .config import AuthorizationServer

WELL_KNOWN_AUTHORIZATION_SERVER = "/.well-known/oauth-authorization-server"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


# ── request parsing ─────────────────────────────────────────────────────────────
def _query(request: Request) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in request.query.items()}


def _form(request: Request) -> dict[str, str]:
    raw = request.body or b""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _pick(src: dict[str, str]) -> dict[str, str]:
    return {k: src[k] for k in consent.OAUTH_PARAM_KEYS if src.get(k)}


# ── response helpers ────────────────────────────────────────────────────────────
def _json_error(error: str, description: str | None = None, *, status: int = 400) -> JSON:
    body: dict[str, Any] = {"error": error}
    if description:
        body["error_description"] = description
    return JSON(body, status_code=status, headers=_NO_STORE)


def _html_error(message: str, *, status: int = 400) -> HTML:
    return HTML(
        f"<!doctype html><meta charset=utf-8><title>Authorization error</title><p>{escape(message)}</p>",
        status_code=status,
    )


def _append_query(url: str, params: dict[str, str]) -> str:
    return url + ("&" if "?" in url else "?") + urlencode(params)


def _redirect_error(redirect_uri: str, state: str | None, error: str, description: str | None = None) -> Redirect:
    params = {"error": error}
    if description:
        params["error_description"] = description
    if state:
        params["state"] = state
    return Redirect(_append_query(redirect_uri, params), status_code=302)


def register_oauth_endpoints(api: BoltAPI, server: AuthorizationServer) -> None:
    """Register the AS discovery, registration, authorize, token, and revoke routes."""

    _mint = sync_to_async(tokens.mint_access_token, thread_sensitive=True)

    def _apply_session_cookie(response: Any, session_key: str) -> Any:
        return response.set_cookie(sessions.session_cookie_name(), session_key, **sessions.session_cookie_kwargs())

    def _token_response(access_token: str, scope: str, refresh_token: str | None = None) -> JSON:
        body: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": server.access_token_ttl,
            "scope": scope,
        }
        if refresh_token:
            body["refresh_token"] = refresh_token
        return JSON(body, headers=_NO_STORE)

    async def _validate_authorize(p: dict[str, str]) -> HTML | Redirect | dict:
        """Validate the authorize request. Returns the client dict on success, or an error
        response. Errors before the redirect_uri is verified are shown inline (never
        redirected — open-redirect guard); after, they redirect back to the client."""
        if p.get("response_type") != "code":
            return _html_error("unsupported response_type (only 'code' is supported)")
        client = await store.get_client(p.get("client_id", ""))
        if client is None:
            return _html_error("unknown client_id")
        redirect_uri = p.get("redirect_uri", "")
        if not server.redirect_uri_allowed(client.get("redirect_uris"), redirect_uri):
            return _html_error("redirect_uri does not match a registered value")
        if p.get("code_challenge_method") != "S256" or not p.get("code_challenge"):
            return _redirect_error(
                redirect_uri, p.get("state"), "invalid_request", "PKCE with S256 code_challenge is required"
            )
        return client

    async def _issue_code_redirect(p: dict[str, str], client: dict, user: dict, *, session_key: str | None) -> Redirect:
        code = await store.create_authorization_code(
            client_pk=client["pk"],
            user_id=user["user_id"],
            redirect_uri=p["redirect_uri"],
            code_challenge=p["code_challenge"],
            code_challenge_method="S256",
            scope=p.get("scope", ""),
            ttl=server.auth_code_ttl,
        )
        params = {"code": code}
        if p.get("state"):
            params["state"] = p["state"]
        resp = Redirect(_append_query(p["redirect_uri"], params), status_code=302)
        if session_key:
            _apply_session_cookie(resp, session_key)
        return resp

    # ── GET /.well-known/oauth-authorization-server ──────────────────────────────
    # Static discovery document: config is frozen at mount, so build it once.
    as_metadata_doc = metadata.authorization_server_metadata(server)

    async def _as_metadata(request: Request):
        return JSON(as_metadata_doc)

    # ── GET {prefix}/authorize ───────────────────────────────────────────────────
    async def _authorize_get(request: Request):
        p = _pick(_query(request))
        result = await _validate_authorize(p)
        if not isinstance(result, dict):
            return result
        client = result
        user = await server.resolve_user(request)
        if user is None:
            return HTML(server.render_login(p))
        # Never issue a code from a CSRF-able GET. Even with auto_consent, an already
        # signed-in user must approve via the Origin-checked POST below, so an attacker
        # can't mint a victim-scoped code by luring the victim to an /authorize GET URL.
        return HTML(server.render_consent(p, client_name=client.get("client_name", ""), username=user["username"]))

    # ── POST {prefix}/authorize ──────────────────────────────────────────────────
    async def _authorize_post(request: Request):
        if not sessions.origin_ok(request, server.effective_issuer()):
            return _html_error("missing or cross-origin Origin header", status=403)
        form = _form(request)
        p = _pick(form)
        result = await _validate_authorize(p)
        if not isinstance(result, dict):
            return result
        client = result

        if form.get("username") is not None:  # login submission
            login = await server.authenticate(form.get("username", ""), form.get("password", ""))
            if login is None:
                return HTML(server.render_login(p, error="Invalid username or password"))
            user = {"user_id": login["user_id"], "username": login["username"]}
            if server.auto_consent:
                return await _issue_code_redirect(p, client, user, session_key=login["session_key"])
            resp = HTML(server.render_consent(p, client_name=client.get("client_name", ""), username=user["username"]))
            return _apply_session_cookie(resp, login["session_key"])

        decision = form.get("decision")
        if decision is not None:  # consent submission
            user = await server.resolve_user(request)
            if user is None:
                return HTML(server.render_login(p, error="Session expired — sign in again"))
            if decision != "approve":
                return _redirect_error(
                    p["redirect_uri"], p.get("state"), "access_denied", "The user denied the request"
                )
            return await _issue_code_redirect(p, client, user, session_key=None)

        return _html_error("missing credentials or consent decision")

    # ── POST {prefix}/token ──────────────────────────────────────────────────────
    async def _token_authorization_code(form: dict[str, str]):
        code = form.get("code")
        if not code:
            return _json_error("invalid_request", "code is required")
        data = await store.consume_authorization_code(code)
        if data is None:
            return _json_error("invalid_grant", "authorization code is invalid, expired, or already used")
        if data["redirect_uri"] != form.get("redirect_uri", ""):
            return _json_error("invalid_grant", "redirect_uri does not match the authorization request")
        client_id = form.get("client_id", "")
        if client_id and data["client_id"] != client_id:
            return _json_error("invalid_grant", "client_id does not match the authorization request")
        if data["code_challenge_method"] != "S256" or not pkce.verify_s256(
            form.get("code_verifier", ""), data["code_challenge"]
        ):
            return _json_error("invalid_grant", "PKCE verification failed")
        user = await server.load_user(data["user_id"])
        if user is None:
            return _json_error("invalid_grant", "the authorizing user no longer exists")
        scope = data["scope"]
        access = await _mint(server, user, scope=scope, client_id=data["client_id"])
        refresh = await store.issue_refresh_token(data["client_pk"], data["user_id"], scope, server.refresh_token_ttl)
        return _token_response(access, scope, refresh)

    async def _token_refresh(form: dict[str, str]):
        raw = form.get("refresh_token")
        if not raw:
            return _json_error("invalid_request", "refresh_token is required")
        status, data = await store.rotate_refresh_token(raw, server.refresh_token_ttl)
        if status != "ok":
            descriptions = {
                "invalid": "unknown refresh token",
                "reuse": "refresh token reuse detected — the session has been revoked",
                "expired": "refresh token has expired",
            }
            return _json_error("invalid_grant", descriptions.get(status, "invalid refresh token"))
        user = await server.load_user(data["user_id"])
        if user is None:
            return _json_error("invalid_grant", "the authorizing user no longer exists")
        scope = data["scope"]
        access = await _mint(server, user, scope=scope, client_id=data["client_id"])
        return _token_response(access, scope, data["raw_new"])

    async def _token(request: Request):
        form = _form(request)
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return await _token_authorization_code(form)
        if grant_type == "refresh_token":
            return await _token_refresh(form)
        return _json_error("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")

    # ── POST {prefix}/register (RFC 7591 DCR) ────────────────────────────────────
    async def _register(request: Request):
        try:
            body = json_decode(request.body or b"{}")
        except msgspec.DecodeError:
            return _json_error("invalid_client_metadata", "request body must be valid JSON")
        if not isinstance(body, dict):
            return _json_error("invalid_client_metadata", "client metadata must be a JSON object")
        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(u, str) for u in redirect_uris)
        ):
            return _json_error("invalid_redirect_uri", "redirect_uris must be a non-empty array of strings")
        client = await store.create_client(
            client_name=body.get("client_name", ""),
            redirect_uris=redirect_uris,
            grant_types=body.get("grant_types") or [],
            scope=body.get("scope", ""),
        )
        return JSON(
            {
                "client_id": client["client_id"],
                "client_name": client["client_name"],
                "redirect_uris": client["redirect_uris"],
                "grant_types": client["grant_types"],
                "token_endpoint_auth_method": client["token_endpoint_auth_method"],
                "scope": client["scope"],
            },
            status_code=201,
            headers=_NO_STORE,
        )

    # ── POST {prefix}/revoke (RFC 7009) ──────────────────────────────────────────
    async def _revoke(request: Request):
        token = _form(request).get("token")
        if token:
            await store.revoke_refresh_token(token)
        return JSON({}, headers=_NO_STORE)

    # ── registration ─────────────────────────────────────────────────────────────
    api.get(WELL_KNOWN_AUTHORIZATION_SERVER, auth=None)(_as_metadata)
    api.get(server.path("authorize"), auth=None)(_authorize_get)
    api.post(server.path("authorize"), auth=None)(_authorize_post)
    api.post(server.path("token"), auth=None)(_token)
    api.post(server.path("revoke"), auth=None)(_revoke)
    if server.allow_dynamic_registration:
        api.post(server.path("register"), auth=None)(_register)

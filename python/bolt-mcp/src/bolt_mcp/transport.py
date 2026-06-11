"""Streamable HTTP transport: POST/GET/DELETE handlers + ``mount_mcp``.

Default servers stream every POST response as a finite SSE message; servers built
with ``MCP(json_response=True)`` return a single ``application/json`` object.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import msgspec

from django_bolt import JSON, BoltAPI, EventSourceResponse, Request, Response, ServerSentEvent
from django_bolt._json import decode as json_decode

from .autoexpose import expose_routes
from .context import McpClientError
from .server import MCP, McpError
from .sessions import SESSION_CLOSE
from .types import (
    INVALID_REQUEST,
    JSONRPC_VERSION,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    Incoming,
    is_request,
)

WELL_KNOWN_PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"

_JSON_CT = "application/json"
_SSE_CT = "text/event-stream"


@dataclass
class ProtectedResource:
    """OAuth 2.1 Resource Server configuration (Tier 2 auth)."""

    resource_url: str
    authorization_servers: list[str] = field(default_factory=list)
    required_scopes: tuple[str, ...] = ()
    token_verifier: Any = None


# ── small helpers ─────────────────────────────────────────────────────────────
# Note: request.headers keys are always lowercase (canonicalized in Rust — see
# src/handler.rs), so handlers read them directly without re-normalizing.
def _accept_types(accept: str) -> set[str]:
    return {part.split(";", 1)[0].strip() for part in accept.split(",") if part.strip()}


def _admits(types: set[str], media: str) -> bool:
    """Whether a parsed Accept set admits ``media``, honoring ``*/*`` and ``type/*`` wildcards.

    An empty set means "accept anything" (RFC 7231), so it admits ``media`` too.
    """
    if not types:
        return True
    main = media.split("/", 1)[0]
    return bool(types & {media, "*/*", f"{main}/*"})


def _accepts(accept: str, media: str) -> bool:
    """Single-shot Accept check; use ``_admits`` directly when the parsed set is reused."""
    return _admits(_accept_types(accept), media)


def _bearer(headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _error(id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": {"code": code, "message": message}}


def _result(id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def _status(status: int, code: int, message: str, *, id: Any = None, headers: dict | None = None) -> JSON:
    return JSON(_error(id, code, message), status_code=status, headers=headers or {})


def _empty(status: int, headers: dict | None = None) -> Response:
    return Response(b"", status_code=status, media_type="text/plain", headers=headers or {})


async def _one_message(envelope: dict):
    yield ServerSentEvent(data=envelope, event="message")


def _respond(mcp: MCP, envelope: dict, headers: dict[str, str]) -> Any:
    """Frame a JSON-RPC envelope as SSE (default) or a single JSON object."""
    if mcp.json_response:
        return JSON(envelope, headers=headers)
    return EventSourceResponse(_one_message(envelope), headers=headers)


async def _frame_tool_stream(items: Any, request_id: Any):
    """Frame a streaming tool's tagged items as SSE ``message`` events.

    Each item is ``("notification", method, params)``, ``("request", id, method, params)``,
    or the terminal ``("result", call_tool_result)``.
    """
    async for item in items:
        kind = item[0]
        if kind == "notification":
            _, method, note_params = item
            yield ServerSentEvent(
                data={"jsonrpc": JSONRPC_VERSION, "method": method, "params": note_params},
                event="message",
            )
        elif kind == "request":
            _, req_id, method, req_params = item
            yield ServerSentEvent(
                data={"jsonrpc": JSONRPC_VERSION, "id": req_id, "method": method, "params": req_params},
                event="message",
            )
        else:  # ("result", call_tool_result)
            _, call_result = item
            yield ServerSentEvent(data=_result(request_id, call_result), event="message")


# ── POST ────────────────────────────────────────────────────────────────────--
async def handle_post(mcp: MCP, request: Request) -> Any:
    headers = request.headers
    accept_types = _accept_types(headers.get("accept", ""))
    has_json = _admits(accept_types, _JSON_CT)
    has_sse = _admits(accept_types, _SSE_CT)

    # 1. Accept negotiation
    if mcp.json_response:
        if not has_json:
            return _status(406, INVALID_REQUEST, "Client must accept application/json")
    elif not (has_json and has_sse):
        return _status(406, INVALID_REQUEST, "Client must accept application/json and text/event-stream")

    # 2. Content-Type (bare media type, ignoring any ;charset parameter)
    if headers.get("content-type", "").split(";", 1)[0].strip() != _JSON_CT:
        return _status(415, INVALID_REQUEST, "Content-Type must be application/json")

    # 3. Parse body
    try:
        raw = json_decode(request.body)
    except msgspec.DecodeError as exc:
        return _status(400, PARSE_ERROR, f"Parse error: {exc}")
    if isinstance(raw, list):
        return _status(400, INVALID_REQUEST, "JSON-RPC batching is not supported")
    try:
        msg = msgspec.convert(raw, Incoming)
    except msgspec.ValidationError as exc:
        return _status(400, INVALID_REQUEST, f"Invalid request: {exc}")

    is_init = msg.method == "initialize"
    stateful = not mcp.stateless
    session = None
    response_headers: dict[str, str] = {}

    # 4. Session + protocol-version validation
    if is_init:
        session = mcp.sessions.create()
        if session.id:
            response_headers["Mcp-Session-Id"] = session.id
    elif stateful:
        sid = headers.get("mcp-session-id")
        if not sid:
            return _status(400, INVALID_REQUEST, "Missing session ID", id=msg.id)
        session = mcp.sessions.get(sid)
        if session is None:
            return _status(404, INVALID_REQUEST, "Invalid or expired session ID", id=msg.id)
        protocol_version = headers.get("mcp-protocol-version")
        if protocol_version is not None and protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return _status(400, INVALID_REQUEST, f"Unsupported protocol version: {protocol_version}", id=msg.id)

    # 5. Client response to a server-initiated request (sampling/elicitation) →
    #    resolve the pending future the tool is awaiting, then 202.
    if msg.method is None and msg.id is not None and not is_init:
        if session is not None:
            future = session.pending.pop(msg.id, None)
            if future is not None and not future.done():
                if msg.error is not None:
                    future.set_exception(McpClientError(msg.error))
                else:
                    future.set_result(msg.result)
        return _empty(202)

    # 6. Notification → 202, no body
    if not is_request(msg):
        if msg.method is not None:
            await mcp.dispatch(msg, session=session, request=request)
        return _empty(202)

    # 7. Request → dispatch. A streaming tool yields an async iterator of tagged items
    #    (each framed as its own SSE event); any other method returns a single result dict.
    try:
        result = await mcp.dispatch(msg, session=session, request=request)
    except McpError as exc:
        return _respond(mcp, _error(msg.id, exc.code, exc.message), response_headers)
    if inspect.isasyncgen(result):
        return EventSourceResponse(_frame_tool_stream(result, msg.id), headers=response_headers)
    return _respond(mcp, _result(msg.id, result), response_headers)


# ── GET (listen channel) ──────────────────────────────────────────────────────
async def handle_get(mcp: MCP, request: Request) -> Any:
    if mcp.stateless or mcp.json_response:
        return _status(405, INVALID_REQUEST, "SSE stream not supported", headers={"Allow": "POST, DELETE"})

    headers = request.headers
    if not _accepts(headers.get("accept", ""), _SSE_CT):
        return _status(406, INVALID_REQUEST, "Client must accept text/event-stream")

    session = mcp.sessions.get(headers.get("mcp-session-id"))
    if session is None:
        return _status(404, INVALID_REQUEST, "Invalid or expired session ID")
    if session.get_stream_open:
        return _status(409, INVALID_REQUEST, "Only one SSE stream is allowed per session")

    session.get_stream_open = True

    async def listen():
        try:
            while True:
                message = await session.queue.get()
                if message is SESSION_CLOSE:
                    return
                yield ServerSentEvent(data=message, event="message")
        finally:
            session.get_stream_open = False

    return EventSourceResponse(listen(), headers={"Mcp-Session-Id": session.id})


# ── DELETE (terminate session) ─────────────────────────────────────────────────
async def handle_delete(mcp: MCP, request: Request) -> Any:
    sid = request.headers.get("mcp-session-id")
    if mcp.stateless or not sid:
        return _status(405, INVALID_REQUEST, "Session termination not supported", headers={"Allow": "POST"})
    if not mcp.sessions.terminate(sid):
        return _status(404, INVALID_REQUEST, "Invalid or expired session ID")
    return _empty(200)


# ── OAuth (Tier 2) ──────────────────────────────────────────────────────────--
def _protected_resource_metadata(oauth: ProtectedResource) -> dict[str, Any]:
    return {
        "resource": oauth.resource_url,
        "authorization_servers": list(oauth.authorization_servers),
        "scopes_supported": list(oauth.required_scopes),
        "bearer_methods_supported": ["header"],
    }


def _oauth_challenge(oauth: ProtectedResource) -> JSON:
    metadata_url = f"{oauth.resource_url.rstrip('/')}{WELL_KNOWN_PROTECTED_RESOURCE}"
    return JSON(
        {"error": "unauthorized", "error_description": "Missing or invalid access token"},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'},
    )


def _check_oauth(oauth: ProtectedResource, request: Request) -> JSON | None:
    token = _bearer(request.headers)
    claims = oauth.token_verifier(token) if (token and oauth.token_verifier) else None
    if claims is None:
        return _oauth_challenge(oauth)
    if oauth.required_scopes:
        granted = set((claims.get("scope") or "").split())
        if not granted.issuperset(oauth.required_scopes):
            return _oauth_challenge(oauth)
    # No Rust auth ran on this path, so request.context is empty. Stash the verified
    # principal on the mutable request.state so per-tool guards (and tools via
    # bolt_mcp.principal) can read it — see server._guards_pass. Best-effort.
    with contextlib.suppress(Exception):
        request.state["context"] = {
            "user_id": claims.get("sub"),
            "is_staff": bool(claims.get("is_staff")),
            "is_superuser": bool(claims.get("is_superuser")),
            "permissions": claims.get("permissions") or [],
            "auth_claims": claims,
        }
    return None


def _resolve_oauth(api: BoltAPI, oauth: Any) -> ProtectedResource | None:
    """Normalize the ``oauth=`` argument to a ``ProtectedResource`` for the /mcp guard.

    A built-in ``AuthorizationServer`` additionally registers the AS discovery/DCR/
    authorize/token endpoints and yields a ProtectedResource that validates the JWTs it
    issues. A plain ``ProtectedResource`` (external IdP) is returned unchanged.
    """
    if oauth is None:
        return None
    from .oauth.config import AuthorizationServer  # noqa: PLC0415 — keep OAuth deps optional/lazy

    if isinstance(oauth, AuthorizationServer):
        from .oauth.endpoints import register_oauth_endpoints  # noqa: PLC0415
        from .oauth.tokens import make_token_verifier  # noqa: PLC0415

        register_oauth_endpoints(api, oauth)
        return ProtectedResource(
            resource_url=oauth.effective_resource_url(),
            authorization_servers=[oauth.effective_issuer()],
            required_scopes=oauth.required_scopes,
            token_verifier=make_token_verifier(oauth),
        )
    if isinstance(oauth, ProtectedResource):
        return oauth
    raise TypeError(f"oauth must be a ProtectedResource or an oauth.AuthorizationServer, got {type(oauth).__name__}")


# ── mount ─────────────────────────────────────────────────────────────────────
def mount_mcp(
    api: BoltAPI,
    mcp: MCP,
    path: str = "/mcp",
    *,
    auth: list[Any] | None = None,
    guards: list[Any] | None = None,
    oauth: Any | None = None,
    expose: Sequence[Callable] | None = None,
) -> None:
    """Register the MCP Streamable HTTP endpoint (POST/GET/DELETE) on ``api``.

    This is the implementation behind the ``api.mount_mcp(mcp, ...)`` method (defined
    on ``BoltAPI`` in django-bolt core); calling either is equivalent.

    By default only native ``@mcp.tool``/``@mcp.resource``/``@mcp.prompt`` components
    are served — existing REST routes are NEVER exposed implicitly. Exposing a route
    makes it callable by any MCP client, so it is an explicit, per-handler opt-in:

        mount_mcp(api, mcp, expose=[get_item, list_users])

    There is intentionally no "expose everything" switch. For deliberate glob/method
    bulk selection, call :func:`expose_routes` directly before mounting.

    Tier 1 auth: pass ``auth=``/``guards=`` — enforced in Rust before the handler.
    Tier 2 auth: pass ``oauth=...`` — Python-side token check + RFC 9728 metadata route +
    ``WWW-Authenticate`` challenge. ``oauth`` accepts either a ``ProtectedResource``
    (validate tokens from an external IdP) or an ``oauth.AuthorizationServer`` (a built-in
    Django-backed OAuth 2.1 server that also serves the discovery/DCR/authorize/token
    endpoints). ``oauth`` and ``auth``/``guards`` are mutually exclusive.
    """
    if expose is True:
        raise TypeError(
            "mount_mcp(expose=...) takes an explicit list of route handlers, not True. "
            "Exposing routes to MCP clients is a security-sensitive, per-handler opt-in "
            "with no expose-everything switch — pass e.g. expose=[get_item]. For "
            "deliberate bulk selection use expose_routes(mcp, api, ...)."
        )
    if expose:
        expose_routes(mcp, api, handlers=expose)  # explicit handler allowlist

    oauth_resource = _resolve_oauth(api, oauth)
    route_auth = None if oauth_resource is not None else auth
    route_guards = None if oauth_resource is not None else guards

    async def _serve(handler: Callable, request: Request) -> Any:
        """Optional Tier-2 OAuth check, then delegate to the transport handler."""
        if oauth_resource is not None and (denied := _check_oauth(oauth_resource, request)) is not None:
            return denied
        return await handler(mcp, request)

    @api.post(path, auth=route_auth, guards=route_guards)
    async def _mcp_post(request: Request):
        return await _serve(handle_post, request)

    @api.get(path, auth=route_auth, guards=route_guards)
    async def _mcp_get(request: Request):
        return await _serve(handle_get, request)

    @api.delete(path, auth=route_auth, guards=route_guards)
    async def _mcp_delete(request: Request):
        return await _serve(handle_delete, request)

    if oauth_resource is not None:
        # Static discovery document: resource config is fixed at mount, so build it once.
        protected_resource_doc = _protected_resource_metadata(oauth_resource)

        @api.get(WELL_KNOWN_PROTECTED_RESOURCE)
        async def _mcp_protected_resource_metadata(request: Request):
            return JSON(protected_resource_doc)

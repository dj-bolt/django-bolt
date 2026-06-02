"""Shared helpers for the MCP test suite (JSON-RPC posting, JWT minting)."""

from __future__ import annotations

import time
from typing import Any

import jwt
import msgspec
from bolt_mcp import MCP, mount_mcp

from django_bolt import BoltAPI

DUAL_ACCEPT = "application/json, text/event-stream"
JSON_CONTENT_TYPE = "application/json"
PROTOCOL = "2025-06-18"


def rpc_body(method: str, params: dict | None = None, *, id: int | str | None = 1) -> bytes:
    """Build a single JSON-RPC message as encoded bytes."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        msg["id"] = id
    if params is not None:
        msg["params"] = params
    return msgspec.json.encode(msg)


def mcp_headers(
    *,
    accept: str | None = DUAL_ACCEPT,
    content_type: str | None = JSON_CONTENT_TYPE,
    session_id: str | None = None,
    protocol: str | None = PROTOCOL,
    authorization: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if accept is not None:
        headers["Accept"] = accept
    if content_type is not None:
        headers["Content-Type"] = content_type
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    if protocol is not None:
        headers["MCP-Protocol-Version"] = protocol
    if authorization is not None:
        headers["Authorization"] = authorization
    return headers


def post_rpc(
    client,
    method: str,
    params: dict | None = None,
    *,
    id: int | str | None = 1,
    session_id: str | None = None,
    accept: str | None = DUAL_ACCEPT,
    content_type: str | None = JSON_CONTENT_TYPE,
    protocol: str | None = PROTOCOL,
    authorization: str | None = None,
    stream: bool = False,
):
    """POST a single JSON-RPC message to /mcp through a TestClient."""
    return client.post(
        "/mcp",
        content=rpc_body(method, params, id=id),
        headers=mcp_headers(
            accept=accept,
            content_type=content_type,
            session_id=session_id,
            protocol=protocol,
            authorization=authorization,
        ),
        stream=stream,
    )


INITIALIZE_PARAMS = {
    "protocolVersion": PROTOCOL,
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "1.0"},
}


def initialize(client, *, authorization: str | None = None):
    """Run the initialize handshake; return (response, session_id)."""
    resp = post_rpc(client, "initialize", INITIALIZE_PARAMS, authorization=authorization)
    session_id = resp.headers.get("mcp-session-id")
    return resp, session_id


def _parse_sse(body: bytes) -> dict:
    """Extract the single JSON-RPC message from a finite SSE response body."""
    text = body.decode("utf-8").replace("\r\n", "\n")
    for block in text.split("\n\n"):
        data = [line[len("data:") :].lstrip() for line in block.split("\n") if line.startswith("data:")]
        if data:
            return msgspec.json.decode("\n".join(data).encode())
    raise AssertionError(f"no SSE data frame found in response: {text!r}")


def _parse_all_sse(body: bytes) -> list[dict]:
    """Return every JSON-RPC message carried by a (finite) SSE response body."""
    text = body.decode("utf-8").replace("\r\n", "\n")
    out: list[dict] = []
    for block in text.split("\n\n"):
        data = [line[len("data:") :].lstrip() for line in block.split("\n") if line.startswith("data:")]
        if data:
            out.append(msgspec.json.decode("\n".join(data).encode()))
    return out


def parse_rpc(resp) -> dict:
    """Decode a JSON-RPC message from a /mcp 200 response.

    Default servers stream SSE (text/event-stream); json_response servers return a
    single application/json object. This handles both so tests are framing-agnostic.
    """
    if "text/event-stream" in resp.headers.get("content-type", ""):
        return _parse_sse(resp.content)
    return msgspec.json.decode(resp.content)


def make_server(*, stateless: bool = False, json_response: bool = False, auth=None, guards=None, oauth=None):
    """Build a BoltAPI + MCP with a standard set of tools/resources/prompts."""
    api = BoltAPI()
    mcp = MCP("test-server", "9.9.9", stateless=stateless, json_response=json_response)

    @mcp.tool
    async def greet(name: str) -> dict:
        """Greet someone by name."""
        return {"greeting": f"Hello, {name}!"}

    @mcp.tool(name="add", description="Add two integers")
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    @mcp.tool
    async def shout(text: str) -> str:
        """Uppercase a string."""
        return text.upper()

    @mcp.tool
    async def boom() -> dict:
        """Always raises — exercises in-band tool errors."""
        raise ValueError("kaboom")

    @mcp.resource("config://app", name="app-config", mime_type="application/json")
    async def app_config() -> str:
        """Static app configuration."""
        return '{"env": "test"}'

    @mcp.prompt
    async def summarize(topic: str) -> str:
        """Summarize a topic."""
        return f"Please summarize: {topic}"

    mount_mcp(api, mcp, auth=auth, guards=guards, oauth=oauth)
    return api, mcp


def mint_jwt(
    secret: str,
    *,
    sub: str = "1",
    permissions: list[str] | None = None,
    is_staff: bool = False,
    is_superuser: bool = False,
    audience: str | None = None,
    issuer: str | None = None,
    extra: dict | None = None,
    exp_in: int = 3600,
) -> str:
    """Mint an HS256 JWT with the claims django-bolt's Rust auth reads."""
    claims: dict[str, Any] = {"sub": sub, "exp": int(time.time()) + exp_in}
    if permissions is not None:
        claims["permissions"] = permissions
    if is_staff:
        claims["is_staff"] = True
    if is_superuser:
        claims["is_superuser"] = True
    if audience is not None:
        claims["aud"] = audience
    if issuer is not None:
        claims["iss"] = issuer
    if extra:
        claims.update(extra)
    return jwt.encode(claims, secret, algorithm="HS256")

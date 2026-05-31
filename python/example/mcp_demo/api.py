"""MCP (Model Context Protocol) server example for django-bolt.

Served over the MCP Streamable HTTP transport at ``/mcp`` via ``bolt-mcp``.
This module is autodiscovered by ``runbolt`` (top-level ``api = BoltAPI()``), so just
run the example server and point an MCP client at it:

    python manage.py runbolt

    # MCP Inspector / Claude Desktop:  http://localhost:8000/mcp

NOTE: ``/mcp`` is NOT a browsable URL. It's a JSON-RPC endpoint driven by HTTP POST;
opening it in a browser issues a GET and will return an error (GET is reserved for the
server→client SSE listen channel). Test it with an MCP client (MCP Inspector, Claude
Desktop) pointed at http://localhost:8000/mcp, or with a curl POST:

    curl -s http://localhost:8000/mcp \\
      -H 'Content-Type: application/json' \\
      -H 'Accept: application/json, text/event-stream' \\
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
           "params":{"protocolVersion":"2025-06-18","capabilities":{},
                     "clientInfo":{"name":"curl","version":"1"}}}'

    # then list tools:
    curl -s http://localhost:8000/mcp \\
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\
      -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

All tools/resources/prompts are served at ``/mcp``. This server is **stateful** (sessions
live in process memory) because the Context's bidirectional features — ``sample`` (use the
client's LLM) and ``elicit`` (ask the user) — require a session and the GET listen channel.
Run a single worker (``runbolt --processes 1``); for ``sample``/``elicit`` use a client that
advertises those capabilities (e.g. MCP Inspector).
"""

from __future__ import annotations

import asyncio
import time

import jwt
import msgspec
from bolt_mcp import MCP, Context
from django.conf import settings
from users.models import User

from django_bolt import (
    BoltAPI,
    HasPermission,
    IsAdminUser,
    IsAuthenticated,
    JWTAuthentication,
    Request,
)
from django_bolt.exceptions import HTTPException

api = BoltAPI()
# Stateful (the default): required by the sample/elicit tools below. Run --processes 1.
mcp = MCP("django-bolt-example", "1.0.0")


@mcp.tool
async def add(a: int, b: int) -> dict:
    """Add two integers."""
    return {"sum": a + b}


@mcp.tool
async def count_users() -> dict:
    """Count users in the database (Django async ORM inside an MCP tool)."""
    return {"count": await User.objects.acount()}


@mcp.tool
async def crunch(steps: int, ctx: Context) -> dict:
    """Streaming tool: emits progress/log notifications, then returns a final result.

    Declare a ``Context`` parameter and call ``ctx.report_progress``/``ctx.info`` as work
    advances, then ``return`` the value. Progress events are sent live over the POST SSE
    stream when the client includes a ``progressToken``. ``sample`` (ask the client's LLM)
    and ``elicit`` (ask the user) live on ``ctx`` too — those need stateful mode (the
    default here; run a single worker).
    """
    for i in range(steps):
        await asyncio.sleep(1)
        await ctx.report_progress(i + 1, steps, message=f"processed {i + 1}/{steps}")
    await ctx.info("done")
    return {"processed": steps}


@mcp.resource("config://example", name="example-config", mime_type="application/json")
async def example_config() -> str:
    """Static example configuration, exposed as an MCP resource."""
    return msgspec.json.encode({"app": "django-bolt-example", "env": "dev"}).decode()


# Resource TEMPLATE: a *parameterized* URI (note the ``{user_id}`` placeholder). Reading
# e.g. ``users://42/profile`` matches this template, extracts ``{user_id}`` from the path
# and coerces it to ``int`` (the annotation) before calling the handler. Clients discover
# it via ``resources/templates/list`` and expand the template themselves — contrast with
# the fixed-URI resource above. The handler's parameters must match the URI's placeholders.
@mcp.resource("users://{user_id}/profile", name="user-profile", mime_type="application/json")
async def user_profile(user_id: int) -> str:
    """A single user's profile, addressed by id (Django async ORM in a templated resource)."""
    user = await User.objects.filter(pk=user_id).afirst()
    if user is None:
        return msgspec.json.encode({"error": f"no user with id {user_id}"}).decode()
    return msgspec.json.encode({"id": user.id, "username": user.username, "email": user.email}).decode()


@mcp.prompt
async def summarize(topic: str) -> str:
    """Prompt template asking the model to summarize a topic."""
    return f"Please write a concise summary of: {topic}"


# Expose an existing django-bolt REST endpoint as an MCP tool (no rewrite needed).
# The tool name comes from the function name ("echo") and the description from the
# docstring. Use @expose_as_tool(name=...) only to override those.
@api.get("/mcp-demo/echo/{message}", tags=["mcp"])
async def echo(message: str) -> dict:
    """Echo a message back to the caller."""
    return {"echo": message}


# ── Interactive tools: the Context's bidirectional features ─────────────────
# sample (use the client's LLM) and elicit (ask the user) send a request to the
# client mid-run and await the reply — they need a stateful session, which is why
# this server isn't stateless. show_settings reads a local resource (no round-trip).
@mcp.tool
async def summarize_with_llm(text: str, ctx: Context) -> dict:
    """Summarize text using the CLIENT's own LLM (MCP sampling)."""
    reply = await ctx.sample(f"Summarize in one sentence:\n\n{text}", max_tokens=200)
    return {"summary": reply["content"]["text"]}


@mcp.tool
async def deploy(target: str, ctx: Context) -> dict:
    """Ask the user to confirm before deploying (MCP elicitation)."""
    answer = await ctx.elicit(
        f"Deploy to {target!r}?",
        schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}},
    )
    if answer.get("action") != "accept":
        return {"deployed": False, "reason": "cancelled by user"}
    return {"deployed": True, "target": target}


@mcp.resource("config://settings", mime_type="application/json")
async def settings_resource() -> str:
    return msgspec.json.encode({"region": "us-east-1", "debug": True}).decode()


@mcp.tool
async def show_settings(ctx: Context) -> dict:
    """Read this server's own resource via the Context (no client round-trip)."""
    return {"settings": await ctx.read_resource("config://settings")}


# ── Auth: token minting + guarded tools ─────────────────────────────────────
# The /mcp endpoint below mounts with auth=[JWTAuthentication()] (no blanket guard),
# so anonymous clients still reach the open tools above, but any Bearer token is
# validated in Rust and its claims land in request.context. Per-tool guards then
# gate individual tools: they're hidden from tools/list and rejected on tools/call
# for principals that fail the guard.
#
# Try it (mint a token, then call a guarded tool):
#
#     TOKEN=$(curl -s http://localhost:8000/mcp-demo/token \
#       -H 'Content-Type: application/json' -d '{"username":"analyst"}' \
#       | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
#
#     curl -s http://localhost:8000/mcp \
#       -H "Authorization: Bearer $TOKEN" \
#       -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
#       -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
#            "params":{"name":"read_report","arguments":{}}}'
#
# "analyst" has reports:read (read_report works, purge_users is denied); "admin" is a
# superuser (everything works); "guest" only passes IsAuthenticated (whoami works).

# A tiny demo directory standing in for a real user store. The claims here are exactly
# the ones django-bolt's Rust auth reads: sub, is_staff, is_superuser, permissions.
_DEMO_PRINCIPALS = {
    "admin": {"is_staff": True, "is_superuser": True, "permissions": ["reports:read"]},
    "analyst": {"is_staff": False, "is_superuser": False, "permissions": ["reports:read"]},
    "guest": {"is_staff": False, "is_superuser": False, "permissions": []},
}


class TokenRequest(msgspec.Struct):
    username: str


@api.post("/mcp-demo/token", tags=["mcp"])
async def mint_token(body: TokenRequest) -> dict:
    """DEMO ONLY: mint a JWT for one of the demo principals (admin/analyst/guest).

    A real app would verify a password and load claims from its user store. Here we
    just sign the demo principal's claims with Django's SECRET_KEY — the same key
    ``JWTAuthentication()`` validates against — and hand back a Bearer token. Send it
    as ``Authorization: Bearer <token>`` on /mcp to authenticate to the guarded tools.
    """
    principal = _DEMO_PRINCIPALS.get(body.username)
    if principal is None:
        raise HTTPException(404, f"unknown demo user {body.username!r}; try one of {sorted(_DEMO_PRINCIPALS)}")
    now = int(time.time())
    claims = {"sub": body.username, "iat": now, "exp": now + 3600, **principal}
    return {"token": jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")}


@mcp.tool(guards=[IsAuthenticated()])
async def whoami(request: Request) -> dict:
    """Return the authenticated principal — requires any valid token (IsAuthenticated)."""
    ctx = request.context or {}
    return {
        "user_id": ctx.get("user_id"),
        "is_staff": ctx.get("is_staff"),
        "is_superuser": ctx.get("is_superuser"),
        "permissions": ctx.get("permissions"),
    }


@mcp.tool(guards=[HasPermission("reports:read")])
async def read_report() -> dict:
    """Return a confidential report — requires the 'reports:read' permission."""
    return {"report": "Q3 revenue up 42%", "classification": "confidential"}


@mcp.tool(guards=[IsAdminUser()])
async def purge_users() -> dict:
    """Admin-only tool — requires a superuser token (IsAdminUser)."""
    return {"purged": 0, "note": "demo: no users were harmed"}


# Everything is served at /mcp. Native @mcp.* components are always served; REST
# routes are exposed only when listed explicitly (no implicit exposure).
# auth=[JWTAuthentication()] validates Bearer tokens (when present) without rejecting
# anonymous callers — per-tool guards above do the gating.
api.mount_mcp(mcp, expose=[echo, mint_token], auth=[JWTAuthentication()])

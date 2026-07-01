"""MCP server example for django-bolt, served over Streamable HTTP at ``/mcp`` via bolt-mcp.

Autodiscovered by ``runbolt`` (top-level ``api = BoltAPI()``):

    python manage.py migrate                 # once: creates the bolt_mcp.oauth tables
    python manage.py runbolt --processes 1    # single worker: sample/elicit need a session

``/mcp`` is protected by a built-in OAuth 2.1 Authorization Server (see the mount at the
bottom). Connect any MCP client that speaks OAuth — it registers itself, sends you to
``/oauth/authorize`` to sign in with your Django credentials, and uses the issued token:

    npx @modelcontextprotocol/inspector       # Streamable HTTP → http://localhost:8000/mcp

or run the headless end-to-end demo: ``python mcp_oauth_client_demo.py``.

``/mcp`` is a JSON-RPC POST endpoint, not a browsable URL — opening it in a browser
issues a GET (reserved for the server→client SSE channel) and returns an error.
"""

from __future__ import annotations

import asyncio

import msgspec
from bolt_mcp import MCP, AuthorizationServer, Context, principal
from users.models import User

from django_bolt import BoltAPI, HasPermission, IsAdminUser, IsAuthenticated, Request

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


# ── Guarded tools ────────────────────────────────────────────────────────────
# After the OAuth login, the access token's claims land on the request. Per-tool guards
# gate individual tools by those claims: a tool whose guard fails is hidden from
# tools/list and rejected on tools/call. The claims come from the Django user via the
# get_extra_claims override below (here: staff users get the "reports:read" permission).
#
#   whoami       — any signed-in user (IsAuthenticated)
#   read_report  — needs the "reports:read" permission  → make the user is_staff
#   purge_users  — needs a superuser (IsAdminUser)       → make the user is_superuser
@mcp.tool(guards=[IsAuthenticated()])
async def whoami(request: Request) -> dict:
    """Return the authenticated principal — requires any signed-in user (IsAuthenticated)."""
    ctx = principal(request)
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
    """Admin-only tool — requires a superuser (IsAdminUser)."""
    return {"purged": 0, "note": "demo: no users were harmed"}


# Serve everything at /mcp behind a built-in OAuth 2.1 Authorization Server. MCP clients
# register dynamically, the user signs in with their Django credentials at /oauth/authorize,
# and the issued token authenticates to /mcp. Native @mcp.* components are always served;
# REST routes (echo) are exposed only when listed explicitly.
#
# Customize it the Django way — subclass and override methods (get_extra_claims,
# render_consent, authenticate, redirect_uri_allowed, …). Config is class attributes
# (or AuthorizationServer(issuer=...) kwargs).
#
# Setup: "bolt_mcp.oauth" in INSTALLED_APPS + `manage.py migrate`, and a Django user to log
# in as. `issuer` must equal the public origin clients reach (it is baked into the tokens
# and discovery documents) — change the port here if you don't run on 8000.
class ExampleMcpAuth(AuthorizationServer):
    # Must equal the exact origin (scheme + host + port) the MCP client connects to —
    # it is baked into every issued token and discovery document, and the client follows
    # it byte-for-byte through the OAuth handshake. This demo is run with
    # `runbolt --port 8001`, so the issuer is 127.0.0.1:8001; change it if you change either.
    issuer = "http://127.0.0.1:8001"

    def get_extra_claims(self, user, *, scopes, client_id):
        # Map the signed-in Django user to the permissions the guarded tools check.
        return {"permissions": ["reports:read"] if user.is_staff else []}


api.mount_mcp(mcp, expose=[echo], oauth=ExampleMcpAuth())

# MCP Server (bolt-mcp)

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) is the open standard for exposing tools, data, and prompts to LLM clients (Claude Desktop, Claude Code, MCP Inspector, …). **bolt-mcp** lets you build an MCP server on top of Django-Bolt and serve it natively over the MCP **Streamable HTTP** transport — driven by Django-Bolt's Rust pipeline, with no Starlette or `mcp`-SDK stack in the request path.

`bolt-mcp` is a **separate, pure-Python package** (it depends on `django-bolt`), released on its own cadence. Install it only when you need MCP.

## Installation

```bash
pip install bolt-mcp
```

Or with uv:

```bash
uv add bolt-mcp
```

Requires `django-bolt` and Python 3.12+. The MCP endpoint is mounted on an ordinary `BoltAPI`, so everything you already use — async ORM, auth, guards, dependencies — works inside tools.

## Quick start

```python
from django_bolt import BoltAPI
from bolt_mcp import MCP

api = BoltAPI()
mcp = MCP("my-server", "1.0.0")


@mcp.tool
async def add(a: int, b: int) -> dict:
    """Add two integers."""
    return {"sum": a + b}


@mcp.resource("config://app", mime_type="application/json")
async def app_config() -> str:
    return '{"env": "prod"}'


@mcp.prompt
async def summarize(topic: str) -> str:
    return f"Please write a concise summary of: {topic}"


api.mount_mcp(mcp)  # serves the MCP Streamable HTTP endpoint at /mcp
```

Run the server and point an MCP client at `http://<host>/mcp`:

```bash
python manage.py runbolt --processes 1
```

`api.mount_mcp(mcp)` is the first-class method on `BoltAPI`. The free function `mount_mcp(api, mcp)` is the underlying implementation and is equivalent if you prefer it.

!!! note "`/mcp` is not a browsable URL"

    `/mcp` is a JSON-RPC endpoint driven by HTTP `POST`. Opening it in a browser issues a `GET`, which is reserved for the server→client listen channel and returns an error. Test it with an MCP client (MCP Inspector, Claude Desktop) or a `curl` `POST`.

### Connecting a client

For a project-local Claude config, drop an `.mcp.json` next to your project:

```json
{
  "mcpServers": {
    "django-bolt": { "type": "http", "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

Or smoke-test the handshake with `curl`:

```bash
curl -s http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1"}}}'
```

## Tools

A tool is a callable an MCP client can invoke. Parameters become the tool's JSON Schema `inputSchema` (derived from type hints via `msgspec`); the return value is mapped to an MCP `CallToolResult`.

```python
@mcp.tool
async def count_users() -> dict:
    """Count users (Django async ORM inside an MCP tool)."""
    return {"count": await User.objects.acount()}
```

Both sync and async functions are supported (sync tools run in a worker thread). Options:

```python
@mcp.tool(
    name="add",                       # defaults to the function name
    title="Add numbers",              # optional human-readable title
    description="Add two integers",   # defaults to the docstring
    output_schema={...},              # optional JSON Schema for the result
    guards=[HasPermission("calc")],   # per-tool authorization (see below)
)
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}
```

**Return values** map as follows:

| Return type | MCP result |
| --- | --- |
| `str` | `content` text |
| `dict` | `content` text **and** `structuredContent` |
| other (Struct, dataclass, list, …) | serialized to text + `structuredContent` |

**Errors are in-band.** A `raise` inside a tool becomes a `CallToolResult` with `isError: true` (the MCP convention), not a transport-level error — so the client sees a normal tool failure it can reason about.

**Accessing the request.** Declare a `request` (or `req`) parameter to receive the Django-Bolt `Request` — useful for reading the authenticated principal via `request.context`. It is injected automatically and excluded from the tool's input schema.

## Resources

Resources expose readable data addressed by URI. The handler returns the resource's text.

```python
import msgspec

@mcp.resource("config://example", name="example-config", mime_type="application/json")
async def example_config() -> str:
    return msgspec.json.encode({"app": "demo", "env": "dev"}).decode()
```

### Resource templates

A URI containing `{var}` placeholders registers a **resource template** — a parameterized resource. Reading a concrete URI extracts the variables, coerces them to the handler's annotated types, and calls the handler. The handler's parameters must match the placeholders exactly.

```python
@mcp.resource("users://{user_id}/profile", name="user-profile", mime_type="application/json")
async def user_profile(user_id: int) -> str:
    user = await User.objects.filter(pk=user_id).afirst()
    if user is None:
        return msgspec.json.encode({"error": f"no user {user_id}"}).decode()
    return msgspec.json.encode({"id": user.id, "username": user.username}).decode()
```

Clients discover templates via `resources/templates/list` and expand them themselves. Reading `users://42/profile` coerces `42` to `int` before calling `user_profile`.

## Prompts

Prompts are reusable message templates. Return a string (becomes a single user message) or a list of message dicts. Arguments are derived from the function signature.

```python
@mcp.prompt
async def summarize(topic: str) -> str:
    """Prompt template asking the model to summarize a topic."""
    return f"Please write a concise summary of: {topic}"
```

## Streaming tools: progress, logging, sampling, elicitation

A tool that declares a `Context` parameter can interact with the client **while it runs**. The `Context` is injected by type annotation (excluded from the input schema, like `request`).

```python
import asyncio
from bolt_mcp import Context

@mcp.tool
async def crunch(steps: int, ctx: Context) -> dict:
    for i in range(steps):
        await asyncio.sleep(1)
        await ctx.report_progress(i + 1, steps, message=f"processed {i + 1}/{steps}")
    await ctx.info("done")
    return {"processed": steps}
```

`ctx.report_progress(...)` and `ctx.debug/info/warning/error(...)` push live `notifications/progress` and `notifications/message` events onto the POST SSE stream as the tool runs, then the return value is sent as the final result. (Progress is only emitted when the client included a `progressToken`.)

The `Context` can also **read this server's own resources** locally (no client round-trip):

```python
@mcp.tool
async def show_settings(ctx: Context) -> dict:
    return {"settings": await ctx.read_resource("config://settings")}
```

### Calling back into the client

`ctx.sample` and `ctx.elicit` send a request **to the client** and await the reply — `sample` asks the client's LLM to generate, `elicit` asks the user for input:

```python
@mcp.tool
async def summarize_with_llm(text: str, ctx: Context) -> dict:
    reply = await ctx.sample(f"Summarize in one sentence:\n\n{text}", max_tokens=200)
    return {"summary": reply["content"]["text"]}


@mcp.tool
async def deploy(target: str, ctx: Context) -> dict:
    answer = await ctx.elicit(
        f"Deploy to {target!r}?",
        schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}},
    )
    if answer.get("action") != "accept":
        return {"deployed": False, "reason": "cancelled by user"}
    return {"deployed": True, "target": target}
```

!!! warning "sample/elicit require stateful streaming + a capable client"

    These are bidirectional: the server sends a request on the SSE stream and the client replies on a separate POST (correlated by id). They require the default **stateful streaming** mode (`MCP(stateless=False, json_response=False)`) run with a **single worker**, and a client that advertised the `sampling`/`elicitation` capability at `initialize` — otherwise they raise (surfaced as an in-band tool error). `report_progress` and logging work in stateless streaming mode too.

## Exposing existing REST routes as tools

You can surface existing Django-Bolt endpoints as MCP tools without rewriting them. Exposure is **explicit and per-handler** — there is no "expose everything" switch, because a stray marker must never silently turn a route into an AI-callable tool.

Pass an allowlist of route handlers to `expose`:

```python
@api.get("/items/{item_id}")
async def get_item(item_id: int) -> dict:
    """Fetch an item by id."""
    return {"id": item_id}


api.mount_mcp(mcp, expose=[get_item])  # tool "get_item", description from the docstring
```

The tool name comes from the function name and the description from the route's OpenAPI description/docstring. Use `@expose_as_tool(name=..., description=...)` only to override those:

```python
from bolt_mcp import expose_as_tool

@api.get("/items/{item_id}")
@expose_as_tool(name="lookup_item", description="Look up an item")
async def get_item(item_id: int) -> dict:
    return {"id": item_id}
```

A handler that isn't a route on the API, that takes `File`/`Form` parameters (can't be represented as JSON tool arguments), or whose name collides with another tool raises `ValueError` rather than being silently dropped or shadowed.

For deliberate bulk selection, call `expose_routes` directly before mounting:

```python
from bolt_mcp import expose_routes

expose_routes(mcp, api, include=["/api/*"], methods=("GET", "POST"))
api.mount_mcp(mcp)
```

## Authentication

### Tier 1 — reuse Django-Bolt auth

Pass `auth` / `guards` to `mount_mcp` — the same authentication and permission classes you use on any route (see [Authentication](authentication.md) and [Permissions](permissions.md)), enforced in Rust before the handler.

`auth` *attempts* to validate a Bearer token but does **not** reject anonymous callers: a valid token's claims land in `request.context`, while requests without one still reach unguarded tools. So a single `/mcp` endpoint can serve both public and protected tools:

```python
from django_bolt import JWTAuthentication

api.mount_mcp(mcp, auth=[JWTAuthentication()])   # validate tokens; don't block anonymous
```

**Per-tool guards** do the gating. A tool whose guard fails is hidden from `tools/list` *and* rejected on `tools/call`, so an anonymous client never even sees the protected tools:

```python
from django_bolt import HasPermission, IsAdminUser, IsAuthenticated, Request

@mcp.tool(guards=[IsAuthenticated()])          # any valid token
async def whoami(request: Request) -> dict:
    return request.context                     # {user_id, is_staff, is_superuser, permissions}

@mcp.tool(guards=[HasPermission("reports:read")])
async def read_report() -> dict:
    return {"report": "Q3 revenue up 42%"}

@mcp.tool(guards=[IsAdminUser()])              # superuser only
async def purge_users() -> dict:
    return {"purged": 0}
```

To require authentication for *every* tool, add a blanket guard at the mount: `api.mount_mcp(mcp, auth=[JWTAuthentication()], guards=[IsAuthenticated()])`.

### Tier 2 — OAuth 2.1 Resource Server

Pass `oauth=ProtectedResource(...)` to enable the [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) protected-resource-metadata route and a `WWW-Authenticate` challenge. The `token_verifier` receives the bearer token and returns claims (or `None` to reject).

```python
from bolt_mcp import ProtectedResource

api.mount_mcp(mcp, oauth=ProtectedResource(
    resource_url="https://api.example.com/mcp",
    authorization_servers=["https://idp.example.com"],
    token_verifier=my_verifier,   # (token: str) -> claims | None
))
```

## Deployment modes

The `MCP(...)` constructor selects how the transport behaves:

| Mode | Constructor | Sessions / GET channel | Live progress & logs | sample / elicit | Multi-worker |
| --- | --- | --- | --- | --- | --- |
| **Stateful streaming** (default) | `MCP(...)` | ✅ | ✅ | ✅ | ❌ single worker |
| **JSON response** | `MCP(json_response=True)` | ✅ | ❌ (final result only) | ❌ | ❌ single worker |
| **Stateless** | `MCP(stateless=True)` | ❌ | ✅ (per-request SSE) | ❌ | ✅ |

- **Stateful streaming** is the default and the most capable — required for `sample`/`elicit` and the GET listen channel. Run with `runbolt --processes 1` (or sticky sessions) so a session always lands on the process that owns it.
- **Stateless** drops sessions entirely (each POST is self-contained), making it safe across multiple worker processes. Use it for plain request/response tools that don't need callbacks.

## Testing

Use Django-Bolt's `TestClient` — it runs requests through the full Rust pipeline in-process, so MCP tools, framing, and auth are exercised end to end:

```python
from django_bolt import BoltAPI
from django_bolt.testing import TestClient
from bolt_mcp import MCP

api = BoltAPI()
mcp = MCP("test-server")

@mcp.tool
async def greet(name: str) -> dict:
    return {"greeting": f"Hello, {name}!"}

api.mount_mcp(mcp)

INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

with TestClient(api) as client:
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT}, headers=HEADERS)
    session_id = resp.headers["mcp-session-id"]

    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={**HEADERS, "Mcp-Session-Id": session_id},
    )
```

See the [Testing guide](testing.md) for more on `TestClient`.

## Supported MCP methods

`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get`, and the Streamable HTTP transport (`POST`/`GET`/`DELETE`) with sessions, both auth tiers, route auto-exposure, and streaming tools (progress / logging / sampling / elicitation) via a tool `Context`.

The server advertises protocol version `2025-06-18` and negotiates with clients on several recent versions.

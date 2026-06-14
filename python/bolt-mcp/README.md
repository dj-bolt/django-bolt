# bolt-mcp

Build [MCP (Model Context Protocol)](https://modelcontextprotocol.io) servers on top of
[django-bolt](https://github.com/FarhanAliRaza/django-bolt), served natively over the MCP
**Streamable HTTP** transport by django-bolt's Rust pipeline — no Starlette/`mcp`-SDK stack.

```python
from django_bolt import BoltAPI
from bolt_mcp import MCP

api = BoltAPI()
mcp = MCP("my-server", "1.0.0")


@mcp.tool
async def greet(name: str) -> dict:
    """Greet someone by name."""
    return {"greeting": f"Hello, {name}!"}


@mcp.resource("config://app", mime_type="application/json")
async def app_config() -> str:
    return '{"env": "prod"}'


@mcp.prompt
async def summarize(topic: str) -> str:
    return f"Please summarize: {topic}"


api.mount_mcp(mcp)  # MCP endpoint mounted at /mcp
```

Point an MCP client (Claude Desktop, MCP Inspector) at `http://<host>/mcp`.

## Transport

`mount_mcp` registers `POST`/`GET`/`DELETE` on `/mcp`:

- **POST** — JSON-RPC requests. By default every request response is streamed as a finite
  `text/event-stream` message (MCP-SDK-faithful). Use `MCP(json_response=True)` to return a single
  `application/json` object instead — the multi-process-friendly mode.
- **GET** — opens the long-lived SSE listen channel for server→client messages (one per session).
- **DELETE** — terminates the session.

Sessions are tracked in-process via `Mcp-Session-Id`. **Stateful mode requires a single worker**
(`runbolt --processes 1`) or sticky sessions; for multiple workers use `MCP(stateless=True)`
(no GET channel, each POST self-contained).

## Streaming tools: progress, logging, sampling, elicitation

A tool that takes a `Context` can stream while it runs: call `ctx.report_progress`/`ctx.info`
as work advances (those become live notifications on the POST SSE stream), then `return` the
final result.

```python
from bolt_mcp import Context

@mcp.tool
async def crunch(n: int, ctx: Context) -> dict:
    for i in range(n):
        await ctx.report_progress(i + 1, n)   # → notifications/progress (if client sent a progressToken)
        await ctx.info("working")             # → notifications/message
    return {"done": n}
```

`ctx` is injected by type annotation (excluded from the tool's input schema, like `request`).
Beyond `report_progress`/`debug`/`info`/`warning`/`error` and `read_resource` (one-way / local),
the Context can call **back into the client and await a reply**:

```python
@mcp.tool
async def assist(text: str, ctx: Context) -> dict:
    summary = await ctx.sample(text)                 # ask the client's LLM (sampling/createMessage)
    ok = await ctx.elicit("Save this summary?")      # ask the user (elicitation/create)
    return {"summary": summary["content"]["text"], "saved": ok["action"] == "accept"}
```

`sample`/`elicit` are bidirectional: the server sends a request on the POST SSE stream and the
client replies on a separate POST (correlated by id). They therefore require **stateful streaming**
(`MCP(stateless=False, json_response=False)`, single worker) and a client that advertises those
capabilities — otherwise they raise (surfaced as an in-band tool error). `report_progress`/logging
work in stateless mode too.

## Expose existing endpoints as tools

Existing REST routes are **never exposed implicitly** — `api.mount_mcp(mcp)` serves only
native `@mcp.tool`/`@mcp.resource`/`@mcp.prompt` components. To expose REST routes, list
their handlers explicitly:

```python
@api.get("/items/{item_id}")
async def get_item(item_id: int) -> dict:
    """Fetch an item by id."""
    return {"id": item_id}


api.mount_mcp(mcp, expose=[get_item])  # tool name "get_item", description from the docstring
```

The tool's name comes from the function name and its description from the route's
description/docstring — no extra decorator needed. Use `@expose_as_tool(name=..., description=...)`
only to override those. A handler that isn't a route on `api`, that takes file/form
parameters, or whose name collides with another tool raises `ValueError` rather than being
silently dropped or shadowed.

Exposure is **per-handler by design**: there is no "expose everything" switch, because a
marker scattered across the codebase must never silently turn a route into an AI-callable
tool. For deliberate bulk selection, call `expose_routes(mcp, api, include=[...], methods=(...))`
explicitly before mounting.

## Authentication

**Tier 1 — reuse django-bolt auth** (validated in Rust before the handler):

```python
from django_bolt import JWTAuthentication, IsAuthenticated

api.mount_mcp(mcp, auth=[JWTAuthentication(secret=...)], guards=[IsAuthenticated()])
```

Per-tool guards: `@mcp.tool(guards=[HasPermission("x")])` — failing tools are filtered from
`tools/list` and rejected on `tools/call`. Tools may declare `request: Request` to read
`request.context` (the authenticated principal).

**Tier 2 — OAuth 2.1 Resource Server** (RFC 9728 metadata + `WWW-Authenticate` challenge):

```python
from bolt_mcp import ProtectedResource

api.mount_mcp(mcp, oauth=ProtectedResource(
    resource_url="https://api.example.com/mcp",
    authorization_servers=["https://idp.example.com"],
    token_verifier=my_verifier,  # (token: str) -> claims | None
))
```

## Development

This package is a uv-workspace member of the django-bolt repo.

```bash
uv sync                                                  # install workspace (editable)
uv run pytest python/bolt-mcp/tests -s -vv        # full suite (incl. subprocess integration)
```

## Status / v1 scope

Implemented: `initialize`/`ping`, `tools/{list,call}`, `resources/{list,read,templates/list}`,
`prompts/{list,get}`, Streamable HTTP (POST/GET/DELETE), sessions, both auth tiers, auto-expose,
and streaming tools (progress/logging/sampling/elicitation) via a tool `Context`.

Not yet (v2): `completion/complete`, `logging/setLevel`, resumability (`Last-Event-ID`), and
Host/Origin DNS-rebinding protection.

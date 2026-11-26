# bolt-mcp

Build [MCP (Model Context Protocol)](https://modelcontextprotocol.io) servers on top of
[django-bolt](https://github.com/FarhanAliRaza/django-bolt), served over the MCP
**Streamable HTTP** transport by the official MCP Rust SDK
([rmcp](https://github.com/modelcontextprotocol/rust-sdk)) embedded in django-bolt's Rust
core — no Starlette or Python-SDK stack in the request path. Dual-era: speaks the
**2026-07-28** revision (stateless, MRTR elicitation, `server/discover`) and the earlier
session-based revisions on the same endpoint.

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

## Long-running tools: tasks

`@mcp.tool(task=True)` runs a call as a task (the MCP Tasks extension, SEP-2663). A 2026-07-28
client that declares `io.modelcontextprotocol/tasks` on its request gets `resultType: "task"` at
once. It polls `tasks/get` for the result, answers `ctx.elicit()` with `tasks/update`, and stops
the task with `tasks/cancel`. Other clients get the result as before.

```python
@mcp.tool(task=True, ttl_ms=600_000, poll_interval_ms=2_000)
async def build_report(month: str, ctx: Context) -> dict:
    await ctx.report_progress(1, 2, "Collecting data")  # sets the statusMessage of the task
    return {"month": month}
```

Cancel, the end of the TTL and `SIGTERM` cancel an `async def` tool. A `def` tool runs in a
thread, which runs to its end. Only the principal that created a task can reach it.
`MCP(max_tasks=1000, max_tasks_per_principal=100)` limits the tasks of a mount and of one
principal. Tasks live in the memory of one process, so `runbolt` refuses to start with
`--processes` above 1, `--max-rss` or `--workers-lifetime` when a mount has a task tool.

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

An exposed route keeps its `auth` and its guards, including the settings defaults. Each tool
call runs them in Rust on the headers of the MCP request, after the auth of the mount. A call
that the route refuses gets `Permission denied for tool '<name>'`, and `tools/list` hides the
tool from that caller. A tool call cannot run middleware, so a route with `@rate_limit`,
Python middleware or Django middleware raises `ValueError`.

The tool result holds the body that HTTP sends for the route. The `response_model`, the return
annotation and the response validation of the route apply, so a field that HTTP removes is not
in the tool result. A route that declares a stream or a file raises `ValueError`. A route that
returns one with no such declaration passes registration, and each call gives an error result.

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
`request.context` (the authenticated principal), `request.user` and the headers of the MCP request.

**Tier 2 — OAuth 2.1 Resource Server** (RFC 9728 metadata + `WWW-Authenticate` challenge):

```python
from bolt_mcp import ProtectedResource

api.mount_mcp(mcp, oauth=ProtectedResource(
    resource_url="https://api.example.com/mcp",
    authorization_servers=["https://idp.example.com"],
    token_verifier=my_verifier,  # (token: str) -> claims | None
))
```

## Tracing

`pip install "bolt-mcp[otel]"` installs `opentelemetry-api`. Then call `instrument(mcp)`
before you register your own middleware:

```python
from bolt_mcp import MCP
from bolt_mcp.otel import instrument

mcp = MCP("my-server", "1.0.0")
instrument(mcp)
```

Each `tools/call`, `resources/read` and `prompts/get` then gets one `SERVER` span, for
example `tools/call greet`, with the attributes of the
[OpenTelemetry semantic conventions for MCP](https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/).
The parent of the span is the `traceparent` in the request `_meta`. A span that a tool makes
is a child of it. A failed call sets `error.type` and the `ERROR` status. For `tools/call`,
Bolt changes a tool exception into an `isError` result first, so `error.type` is `tool_error`.
For `resources/read` and `prompts/get`, `error.type` is the class name of the exception.
`mcp.resource.uri` never has userinfo. The span records the tool arguments, the tool
result, the exception text and the query values of a resource URI only when
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Without it, each query value is
`REDACTED`. The tool name, the prompt name and the URI path always go in the span. `bolt_mcp` does not import
OpenTelemetry, so a server that does not call `instrument()` does not change.

## Development

This package is a uv-workspace member of the django-bolt repo.

```bash
uv sync                                                  # install workspace (editable)
uv run pytest python/bolt-mcp/tests -s -vv        # full suite (incl. subprocess integration)
```

## Status / scope (0.2)

The protocol core is the official MCP Rust SDK (rmcp), embedded in django-bolt >= 0.10 —
protocol revisions `2024-11-05` through `2026-07-28` are served dual-era. Implemented on
top of it: tools/resources/prompts with msgspec schemas, MRTR elicitation with replay and
signed `requestState`, per-request log gating, SEP-2549 `ttlMs`/`cacheScope` hints,
SEP-2243 routing-header validation, the SEP-2663 tasks extension, Rust-evaluated per-tool
guards, all three auth tiers (with RFC 9207 `iss` and DCR `application_type`), auto-expose,
localhost-only Host validation by default, and configurable Host/Origin DNS-rebinding
protection (`mount_mcp(allowed_hosts=..., allowed_origins=...)`).

Not yet: `completion/complete`, `notifications/tasks`, list-changed notifications (catalogs
are static), and CIMD client registration in the built-in Authorization Server.

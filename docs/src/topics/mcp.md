# MCP Server (bolt-mcp)

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) is the open standard for exposing tools, data, and prompts to LLM clients (Claude Desktop, Claude Code, MCP Inspector, …). **bolt-mcp** lets you build an MCP server on top of Django-Bolt, served over the MCP **Streamable HTTP** transport by the official MCP Rust SDK ([rmcp](https://github.com/modelcontextprotocol/rust-sdk)) embedded in Django-Bolt's Rust core — no Starlette or Python-SDK stack in the request path.

The server is **dual-era**: it speaks the current **2026-07-28** revision (stateless, per-request metadata, MRTR elicitation, `server/discover`, required routing headers) *and* the earlier session-based revisions (`initialize` handshake, `Mcp-Session-Id`) on the same endpoint. 2026-07-28 requests are always served statelessly, so they are multi-worker safe.

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
    guards=[Requires("permissions", "calc")],   # per-tool authorization (see below)
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

**Accessing the request.** Declare a `request` (or `req`) parameter to receive the Django-Bolt `Request` — injected automatically and excluded from the tool's input schema. Read the authenticated principal with `bolt_mcp.principal(request)`, which returns `{user_id, is_staff, is_superuser, permissions, auth_claims}` and works under **every** auth tier (Tier 1 fills `request.context`; the OAuth tiers stash it on `request.state` — `principal()` reads both, so prefer it over `request.context` directly).

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

`ctx.report_progress(...)` pushes live `notifications/progress` events onto the POST SSE stream as the tool runs, then the return value is sent as the final result. (Progress is only emitted when the client included a `progressToken`.)

`ctx.debug/info/warning/error(...)` emit `notifications/message` log lines. For **2026-07-28 clients**, they are sent on the request's own stream, but only when the request carried `io.modelcontextprotocol/logLevel` in `_meta` — and only at or above that severity (per spec). For **legacy session clients**, they are delivered on the session's standalone GET stream, following the classic session model. Logging is deprecated by the 2026-07-28 spec (SEP-2577) but stays fully functional.

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

How `elicit` reaches the client depends on the client's protocol era:

- **2026-07-28 clients** use **MRTR** (Multi Round-Trip Requests): the tool call ends with an `input_required` result carrying the elicitation request and a signed, opaque `requestState`; the client gathers the answer and retries the call with `inputResponses` attached. **The tool function re-runs from the top on each retry** — earlier `elicit` calls return instantly from the replayed answers. This works statelessly across any number of workers.
- **Legacy session clients** get the live path: the server sends the request on the tool call's SSE stream and the tool suspends until the client replies on a separate POST. This requires the default stateful mode and a single worker.

!!! warning "MRTR replay: write idempotent pre-elicit code"

    On 2026-07-28 clients, code **before** an `elicit` call runs once per round trip. Keep side effects after the last `elicit`, make them idempotent, or guard them with `ctx.is_replay`. If the *sequence* of `elicit` calls is not deterministic for the same arguments, name each input point with `ctx.elicit(..., key="...")`. The `requestState` is HMAC-sealed (keyed from `SECRET_KEY`) and bound to the tool, its arguments, and the authenticated principal — a tampered or replayed state is rejected.

!!! note "sampling is deprecated"

    `ctx.sample` (asking the client's LLM to generate) is deprecated by the 2026-07-28 spec (SEP-2577). It still works for legacy session clients that advertised the `sampling` capability; on 2026-07-28 clients it raises, surfaced as an in-band tool error — integrate with an LLM provider API directly instead. A client that never advertised the capability also gets a clear in-band error.

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

bolt-mcp offers three layers, smallest to largest:

- **Tier 1** reuses Django-Bolt's own authentication + per-tool guards (you mint tokens).
- **Tier 2** turns the server into an OAuth 2.1 *Resource Server* that validates tokens issued by an external IdP.
- **Tier 3** makes it a full OAuth 2.1 *Authorization Server* that issues its own tokens — so OAuth-native clients (Claude.ai, ChatGPT, the Claude Code CLI) link once and refresh silently.

### Tier 1 — reuse Django-Bolt auth

Pass `auth` / `guards` to `mount_mcp` — the same authentication and permission classes you use on any route (see [Authentication](authentication.md) and [Permissions](permissions.md)), enforced in Rust before the handler.

`auth` *attempts* to validate a Bearer token but does **not** reject anonymous callers: a valid token's claims land in `request.context`, while requests without one still reach unguarded tools. So a single `/mcp` endpoint can serve both public and protected tools:

```python
from django_bolt import JWTAuthentication

api.mount_mcp(mcp, auth=[JWTAuthentication()])   # validate tokens; don't block anonymous
```

**Per-tool guards** do the gating. A tool whose guard fails is hidden from `tools/list` *and* rejected on `tools/call`, so an anonymous client never even sees the protected tools:

```python
from django_bolt import IsAuthenticated, Request, Requires

@mcp.tool(guards=[IsAuthenticated()])          # any valid token
async def whoami(request: Request) -> dict:
    return request.context                     # {user_id, is_staff, is_superuser, permissions}

@mcp.tool(guards=[Requires("permissions", "reports:read")])
async def read_report() -> dict:
    return {"report": "Q3 revenue up 42%"}

@mcp.tool(guards=[Requires("is_superuser", True)])  # superuser only
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

### Tier 3 — built-in OAuth 2.1 Authorization Server

Tiers 1 and 2 still leave token *issuance* to you. Tier 3 makes Django-Bolt the **Authorization Server** itself, so OAuth-native clients can link your server, run the OAuth flow **once**, and refresh silently — no pasting tokens, no re-adding the connector. It implements the full MCP authorization handshake end to end:

1. Client hits `/mcp` with no token → `401` + `WWW-Authenticate: Bearer resource_metadata="…"`
2. Client fetches **Protected Resource Metadata** ([RFC 9728](https://www.rfc-editor.org/rfc/rfc9728)) → discovers the authorization server
3. Client fetches **Authorization Server Metadata** ([RFC 8414](https://www.rfc-editor.org/rfc/rfc8414)) → discovers the endpoints
4. Client **registers itself** via Dynamic Client Registration ([RFC 7591](https://www.rfc-editor.org/rfc/rfc7591))
5. **Authorization Code + PKCE** (S256): the user signs in with their Django credentials and consents
6. Client exchanges the code for an **access token + refresh token**, then refreshes silently

Issued tokens are `HS256` JWTs signed with Django's `SECRET_KEY`, carrying the exact claims Django-Bolt's auth already reads (`sub`, `is_staff`, `is_superuser`, `permissions`, plus `scope`/`iss`/`aud`/`jti`). So they validate on `/mcp` and drive the same per-tool `guards` as Tier 1 — no extra verifier code.

#### Setup

`bolt_mcp.oauth` is a Django app (it persists registered clients, authorization codes, and refresh tokens). Add it to `INSTALLED_APPS` and migrate:

```python
# settings.py
INSTALLED_APPS = [..., "bolt_mcp.oauth"]
```

```bash
python manage.py migrate
```

Then mount with an `AuthorizationServer`:

```python
from bolt_mcp.oauth import AuthorizationServer

api.mount_mcp(mcp, oauth=AuthorizationServer(issuer="https://api.example.com"))
```

Users sign in at `/oauth/authorize` with their **Django credentials** (Django's own session framework + password hashing). Because the `401` challenge is what triggers OAuth discovery, the **entire `/mcp` endpoint requires a valid token** under Tier 3 — there are no anonymous tools (unlike Tier 1). Read the principal in a tool with `principal(request)`:

```python
from bolt_mcp import principal

@mcp.tool(guards=[IsAuthenticated()])
async def whoami(request: Request) -> dict:
    return principal(request)   # {user_id, is_staff, is_superuser, permissions, auth_claims}
```

!!! warning "`issuer` must match the public URL exactly"

    The `issuer` is baked into every token (`iss`/`aud`) and into the discovery documents, and clients compare it byte-for-byte. It must equal the scheme + host (+ port) clients actually reach. If unset it defaults to `http://localhost:8000` (dev only, with a warning) — set it explicitly anywhere else, including behind a reverse proxy.

#### Customizing — subclass and override (Django CBV style)

`AuthorizationServer` is configured like a Django class-based view: **class attributes** for config, **method overrides** for behavior.

```python
class MyMcpAuth(AuthorizationServer):
    issuer = "https://api.example.com"
    access_token_ttl = 1800

    # add claims to every token — sees the requested scopes + the calling client
    def get_extra_claims(self, user, *, scopes, client_id):
        return {"tenant_id": user.profile.tenant_id, "roles": [g.name for g in user.groups.all()]}

    # render your own login / consent pages
    def render_consent(self, params, *, client_name, username):
        return my_template.render(...)

    # swap the credential check (MFA, an external user store, …)
    async def authenticate(self, username, password):
        ...

api.mount_mcp(mcp, oauth=MyMcpAuth())
```

For trivial cases set attributes inline instead of subclassing: `AuthorizationServer(issuer="…", access_token_ttl=1800)`.

**Configuration attributes**

| Attribute | Default | Purpose |
| --- | --- | --- |
| `issuer` | `http://localhost:8000` (dev) | Public origin; token `iss`/`aud` + discovery base. Set in prod. |
| `resource_url` | `issuer` | OAuth resource identifier / token audience. |
| `scopes_supported` | `("mcp",)` | Scopes advertised in metadata. |
| `required_scopes` | `()` | Scopes a token must carry to call `/mcp`. |
| `access_token_ttl` | `3600` | Access-token lifetime (seconds). |
| `refresh_token_ttl` | `2592000` | Refresh-token lifetime (30 days). |
| `auth_code_ttl` | `300` | Authorization-code lifetime. |
| `jwt_secret` / `jwt_algorithm` | `SECRET_KEY` / `HS256` | Signing key & algorithm. |
| `auto_consent` | `False` | Skip the consent screen once signed in. |
| `allow_dynamic_registration` | `True` | Enable `/oauth/register` (DCR). |
| `endpoint_prefix` | `/oauth` | Path prefix for authorize / token / register / revoke. |

**Overridable methods**

| Method | Called | Override to |
| --- | --- | --- |
| `get_extra_claims(user, *, scopes, client_id)` | minting a token | add tenant / role / plan claims |
| `authenticate(username, password)` *(async)* | login POST | change credential checking / add MFA |
| `resolve_user(request)` *(async)* | every `/authorize` | change how the session maps to a user |
| `load_user(user_id)` *(async)* | minting a token | change how the user is loaded |
| `render_login(params, *, error=None)` | no session | custom sign-in HTML |
| `render_consent(params, *, client_name, username)` | signed in | custom consent HTML |
| `redirect_uri_allowed(registered, redirect_uri)` | `/authorize` | change redirect-URI matching |

#### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/.well-known/oauth-authorization-server` | AS metadata (RFC 8414) |
| `GET` | `/.well-known/oauth-protected-resource/mcp` | resource metadata (RFC 9728, path-aware for the `/mcp` resource) |
| `POST` | `/oauth/register` | Dynamic Client Registration (RFC 7591) |
| `GET` / `POST` | `/oauth/authorize` | login + consent (Authorization Code + PKCE) |
| `POST` | `/oauth/token` | `authorization_code` and `refresh_token` grants |
| `POST` | `/oauth/revoke` | token revocation (RFC 7009) |

#### Security

Secure by default: **PKCE S256 required** (`plain` rejected), **exact redirect-URI matching** (no open redirect), **single-use** authorization codes (atomic consume), **refresh-token rotation with reuse detection** (replaying a rotated token revokes the whole chain), authorization codes and refresh tokens **stored only as SHA-256 hashes**, an **Origin-header CSRF check** on the browser consent POST, and Django's own password hashing + session framework for login. All state lives in the database, so client registrations and refresh tokens survive restarts and are shared across worker processes.

#### Connecting an OAuth client

=== "Claude Code CLI"

    The CLI speaks OAuth and can reach `localhost`:

    ```bash
    claude mcp add --transport http my-server http://localhost:8000/mcp
    ```

    Then run `/mcp` in the TUI → **Authenticate**. A browser opens at `/oauth/authorize`; sign in and click **Allow**. Tokens are stored and refreshed automatically.

=== "Claude.ai / ChatGPT"

    Add a **custom connector** pointing at `https://your-domain.com/mcp`. These run server-side and **cannot reach `localhost`** — expose a public HTTPS URL (e.g. a `cloudflared`/`ngrok` tunnel) and set `issuer` to that exact URL.

=== "MCP Inspector"

    ```bash
    npx @modelcontextprotocol/inspector
    ```

    Transport **Streamable HTTP**, URL `http://localhost:8000/mcp`, **Connect** → it registers itself and runs the OAuth flow.

#### Prefer an external IdP?

If you don't want to be the authorization server, stay a pure Resource Server (Tier 2): point `ProtectedResource(authorization_servers=[...], token_verifier=...)` at Auth0 / WorkOS / Keycloak / Entra and let it issue the tokens.

## Deployment modes

**2026-07-28 clients are always served statelessly** — every request is self-contained, MRTR elicitation works, and any worker can answer it. The `MCP(...)` constructor selects how **legacy** (session-era) clients are served:

| Mode | Constructor | Legacy sessions / GET channel | Live progress | Live sample / elicit (legacy) | Multi-worker |
| --- | --- | --- | --- | --- | --- |
| **Stateful streaming** (default) | `MCP(...)` | ✅ | ✅ | ✅ | ❌ single worker¹ |
| **JSON response** | `MCP(json_response=True)` | ❌ (sessionless) | ✅ (SSE fallback) | ❌ | ✅ |
| **Stateless** | `MCP(stateless=True)` | ❌ | ✅ (per-request SSE) | ❌ | ✅ |

¹ Only legacy sessions pin a process. Modern traffic on the same mount is multi-worker safe regardless.

- **Stateful streaming** is the default and the most capable for legacy clients — required for live `sample`/`elicit` and the GET listen channel. Run with `runbolt --processes 1` (or sticky sessions) so a session always lands on the process that owns it.
- **JSON response** answers legacy POSTs with a single `application/json` object when the response is just a result; a tool that emits notifications first automatically falls back to SSE so they are preserved.
- **Stateless** drops legacy sessions entirely. Use it for plain request/response tools that don't need legacy callbacks.

`mount_mcp(..., allowed_hosts=[...], allowed_origins=[...])` opts into the transport's DNS-rebinding protection (403 on Host/Origin mismatch). For a server bound to localhost, pass `allowed_hosts=["localhost", "127.0.0.1", "::1"]`; deployed servers normally pin Host/Origin at the proxy.

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

The protocol is implemented by the official MCP Rust SDK (rmcp) embedded in Django-Bolt. Served methods: `server/discover`, `tools/list`, `tools/call` (with MRTR `input_required` results), `resources/list`, `resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get`, `subscriptions/listen` — plus, for legacy-era clients, `initialize`, `ping`, and the session `GET`/`DELETE` endpoints. List results carry the SEP-2549 `ttlMs`/`cacheScope` caching hints (configure with `MCP(list_ttl_ms=..., list_cache_scope=...)`), and the SEP-2243 routing headers (`Mcp-Method`, `Mcp-Name`, `Mcp-Param-*`) are validated against the body. With the built-in Authorization Server (Tier 3) it additionally serves `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`, and `/oauth/{register,authorize,token,revoke}`, with RFC 9207 `iss` on authorization responses. The advertised `resource` (and token audience) is the full MCP endpoint URL — issuer + mount path.

The server advertises protocol version **2026-07-28** and negotiates every earlier revision back to `2024-11-05` with legacy clients.

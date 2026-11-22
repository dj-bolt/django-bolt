---
description: Build an MCP server on a Bolt API with bolt-mcp, with tools, resources, prompts, progress, middleware, authentication and tests.
---

# MCP servers

The [Model Context Protocol](https://modelcontextprotocol.io) (MCP) lets an LLM client call your code.
A client, for example Claude Code or MCP Inspector, reads the list of your tools and calls them for the model.
The `bolt-mcp` package serves an MCP server from a `BoltAPI`, on the same port as your routes.

Bolt serves the MCP Streamable HTTP transport in its Rust core, with the official MCP Rust SDK (rmcp).
Your tools, resources and prompts are Python functions.

## Install bolt-mcp

```bash
pip install "django-bolt[mcp]"
```

`bolt-mcp` is a separate package with its own version. The `mcp` extra installs it.

## Create a server

Create an `MCP` object, register functions on it, and mount it on the API:

```python title="blog/api.py"
from bolt_mcp import MCP

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("blog", "1.0.0", instructions="Tools for the articles of the blog.")

ARTICLES = {
    1: {"id": 1, "title": "Hello Bolt", "published": True},
    2: {"id": 2, "title": "Draft notes", "published": False},
}


@mcp.tool
async def get_article(article_id: int) -> dict:
    """Return one article by its id."""
    return ARTICLES[article_id]


@mcp.resource("blog://about", mime_type="text/plain")
async def about() -> str:
    """A short description of the blog."""
    return "A blog about Django-Bolt."


@mcp.prompt
async def summarize_article(article_id: str) -> str:
    """Ask the model for a summary of one article."""
    return f"Summarize the article with id {article_id} in two sentences."


api.mount_mcp(mcp)
```

`api.mount_mcp(mcp)` serves the MCP endpoint at `/mcp`.
Start the server, and send an `initialize` request to check it:

```bash
python manage.py runbolt
```

```bash
curl localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

```text
data: 
id: 0
retry: 3000

data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"logging":{},"prompts":{},"resources":{},"tools":{}},"serverInfo":{"name":"blog","version":"1.0.0"},"instructions":"Tools for the articles of the blog."}}

```

`/mcp` is not a page for a browser.
A `GET` from a browser gets `406 Not Acceptable`.

## Connect a client

Give the client the URL of the endpoint, for example `http://127.0.0.1:8000/mcp`.

=== "Claude Code"

    ```bash
    claude mcp add --transport http blog http://127.0.0.1:8000/mcp
    ```

    Or put a `.mcp.json` file in the root of your project:

    ```json
    {
      "mcpServers": {
        "blog": {"type": "http", "url": "http://127.0.0.1:8000/mcp"}
      }
    }
    ```

=== "MCP Inspector"

    ```bash
    npx @modelcontextprotocol/inspector
    ```

    Select the transport **Streamable HTTP**, enter the URL and click **Connect**.

By default, the endpoint accepts only the `Host` values `localhost`, `127.0.0.1` and `[::1]`.
On a public server, set `allowed_hosts`. See [Deploy the server](#deploy-the-server).

## Write a tool

A tool is a function that the client can call.
Register it with `@mcp.tool`:

```python
@mcp.tool(annotations={"readOnlyHint": True})
async def list_articles(published: bool = True) -> list[dict]:
    """List the articles. Set published to false for the drafts."""
    return [a for a in ARTICLES.values() if a["published"] == published]
```

The client sees this entry in `tools/list`:

```json
{"name": "list_articles", "description": "List the articles. Set published to false for the drafts.", "inputSchema": {"properties": {"published": {"type": "boolean", "default": true}}, "type": "object"}, "annotations": {"readOnlyHint": true}}
```

The type hints of the arguments give the JSON Schema of the input.
The docstring is the description. The model reads it, so write what the tool does and when to use it.
Bolt converts and checks the arguments with msgspec.
A bad argument gives an error result. Its text starts with `Invalid arguments:` and names the field.

A `def` tool runs on a worker thread. An `async def` tool runs on the event loop.

`@mcp.tool` takes these options:

| Option | Default | Effect |
| --- | --- | --- |
| `name` | the function name | The tool name. `@mcp.tool("find")` also sets it. |
| `title` | `None` | A name for people. |
| `description` | the docstring | The description for the model. |
| `output_schema` | `None` | A JSON Schema dict of the structured result, or `"auto"` to read it from the return type. |
| `annotations` | `None` | MCP tool annotations, for example `{"readOnlyHint": True}`. |
| `icons` | `None` | A list of MCP icon dicts. |
| `meta` | `None` | Sent as `_meta` of the tool in `tools/list`, for example `{"ui": {"resourceUri": "ui://blog/view.html"}}` for MCP Apps. |
| `guards` | `[]` | Guards for this tool. See [Require authentication](#require-authentication). |

`output_schema="auto"` works for a return type that gives a JSON object, for example a `msgspec.Struct`.
For any other type it gives no schema.

### Return a result

Bolt maps the return value to the MCP result:

| Return value | Result |
| --- | --- |
| `str` | One text content |
| `dict` | A text content with the JSON of the dict, and `structuredContent` |
| Another value, for example a `msgspec.Struct` or a `list` | Its JSON as text, and `structuredContent`. A value that is not an object goes in `{"result": ...}`. |
| `ToolResult` | Its `content`, `structured` and `meta`. See below. |

For `get_article(article_id=1)`, the result is:

```json
{"content": [{"type": "text", "text": "{\"id\":1,\"title\":\"Hello Bolt\",\"published\":true}"}], "structuredContent": {"id": 1, "published": true, "title": "Hello Bolt"}, "isError": false}
```

Return a `ToolResult` to give the model a short text and the client the full data:

```python
from bolt_mcp import ToolResult


@mcp.tool
async def article_stats(article_id: int) -> ToolResult:
    """Count the words of an article."""
    words = len(ARTICLES[article_id]["title"].split())
    return ToolResult(
        content=f"The article has {words} words.",
        structured={"article_id": article_id, "words": words},
    )
```

```json
{"content": [{"type": "text", "text": "The article has 2 words."}], "structuredContent": {"article_id": 1, "words": 2}, "isError": false}
```

`content` is a string or a list of MCP content blocks. `meta` becomes the `_meta` of the result.
`output_schema="auto"` cannot read a schema from `ToolResult`, so it raises `TypeError`. Give a dict.
Bolt does not check a result against `output_schema`.

### Report an error

A tool error is part of the result: `isError` is `true`. The model can read it and try again.
Raise `ToolError` for a message that the client may read:

```python
from bolt_mcp import ToolError


@mcp.tool
async def find_article(article_id: int) -> dict:
    """Return one article, or an error when it does not exist."""
    article = ARTICLES.get(article_id)
    if article is None:
        raise ToolError(f"No article with id {article_id}.")
    return article
```

```json
{"content": [{"type": "text", "text": "No article with id 5."}], "isError": true}
```

Bolt hides the text of each other exception, because it can contain passwords, hosts or file paths.
The client gets `Tool 'get_article' failed.`, and Bolt logs the traceback on the `bolt_mcp._execute` logger.
For example, `get_article(article_id=9)` raises `KeyError`, and the client sees only that message.

To send the exception text during development, use `MCP("blog", mask_error_details=False)`.
Do not use it in production.

### Read the request

A tool with a `request` or `req` argument gets the Bolt `Request`.
The argument is not in the input schema.
`bolt_mcp.principal(request)` gives the authenticated caller:

```python
from bolt_mcp import principal

from django_bolt import Request


@mcp.tool
async def whoami(request: Request) -> dict:
    """Return the caller."""
    return principal(request)
```

For a caller with a JWT of the user `ada`, the structured result is:

```json
{"user_id": "1", "is_staff": false, "is_superuser": false, "auth_backend": "jwt", "auth_backend_index": 0, "auth_claims": {"sub": "1", "exp": 1795316703, "iat": 1795313103, "is_staff": false, "is_superuser": false, "username": "ada"}}
```

For an anonymous caller, `principal()` gives `{}`.

## Write a resource

A resource is data that the client reads by its URI. The function returns the text:

```python
@mcp.resource("blog://style-guide", name="style-guide", mime_type="text/markdown")
async def style_guide() -> str:
    """The writing rules of the blog."""
    return "# Style guide\n\nUse short sentences."
```

`@mcp.resource(uri, *, name=None, mime_type="text/plain", description=None, meta=None)` takes these options.
`name` defaults to the function name, and `description` to the docstring.
`meta` is sent as `_meta` of the contents. MCP Apps read settings from it, for example `{"ui": {"csp": {...}}}`.

### Read a resource with parameters

A URI with `{name}` parts registers a resource template.
The function arguments must have the same names as the parts:

```python
import msgspec


@mcp.resource("articles://{article_id}", mime_type="application/json")
async def article_resource(article_id: int) -> str:
    """One article as JSON."""
    return msgspec.json.encode(ARTICLES[article_id]).decode()
```

The client finds the template with `resources/templates/list`.
A read of `articles://1` converts `1` to `int` and gives:

```json
{"contents": [{"uri": "articles://1", "mimeType": "application/json", "text": "{\"id\":1,\"title\":\"Hello Bolt\",\"published\":true}"}]}
```

Each part matches one path segment. Different names in the URI and the function raise `ValueError`.

A resource error is a JSON-RPC error, not a result.
Bolt hides the text of an exception and logs it on the `bolt_mcp._dispatch` logger.
The client gets `Resource 'articles://9' failed.`

## Write a prompt

A prompt is a message template that the user selects in the client:

```python
@mcp.prompt
async def review_article(title: str, tone: str = "friendly") -> str:
    """Ask for a review of an article."""
    return f"Review the article '{title}' in a {tone} tone."
```

A string becomes one user message.
A function can also return a list of MCP message dicts, for example `[{"role": "user", "content": {"type": "text", "text": "..."}}]`.
The arguments come from the signature. An argument with no default is required.

!!! note
    An MCP client sends each prompt argument as a string.
    Declare each argument as `str`. An `int` argument refuses the string `"1"` with an invalid-params error.

## Report progress and log messages

Add a `Context` argument to a tool. The argument is not in the input schema:

```python
import asyncio

from bolt_mcp import Context


@mcp.tool
async def reindex(ctx: Context) -> dict:
    """Rebuild the search index of the articles."""
    for step in range(1, 4):
        await asyncio.sleep(0.5)
        await ctx.report_progress(step, 3, message=f"Batch {step} of 3")
    await ctx.info("Index rebuilt")
    return {"indexed": len(ARTICLES)}
```

The client gets each notification while the tool runs, then the result:

```text
data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"message":"Batch 1 of 3","progress":1.0,"progressToken":"p1","total":3.0}}

data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"message":"Batch 2 of 3","progress":2.0,"progressToken":"p1","total":3.0}}

data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"message":"Batch 3 of 3","progress":3.0,"progressToken":"p1","total":3.0}}

data: {"jsonrpc":"2.0","method":"notifications/message","params":{"data":"Index rebuilt","level":"info"}}

data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"indexed\":2}"}],"structuredContent":{"indexed":2},"isError":false}}
```

- `ctx.report_progress(progress, total=None, message=None)` sends a progress notification. Bolt drops it when the client sent no `progressToken`.
- `ctx.debug()`, `ctx.info()`, `ctx.warning()` and `ctx.error()` send a log message. A client of the 2026-07-28 revision gets it only when its request sets a log level, and only at that level or above.
- `ctx.read_resource(uri)` reads a resource of this server and returns its text.
- `ctx.meta` is the `_meta` object of the request.

## Ask the user for input

`ctx.elicit(message, schema=None)` asks the user a question in the client:

```python
@mcp.tool
async def publish_article(article_id: int, ctx: Context) -> dict:
    """Publish an article after the user confirms."""
    answer = await ctx.elicit(f"Publish article {article_id}?")
    if answer["action"] != "accept":
        return {"published": False}
    return {"published": True, "article_id": article_id}
```

The answer has an `action`: `accept`, `decline` or `cancel`.
To ask for data, give a JSON Schema of an object as `schema`. The data is then in `answer["content"]`.

The way this works depends on the protocol revision of the client:

- A 2026-07-28 client: the tool call ends with an `input_required` result. The client asks the user, then calls the tool again with the answer. **The tool runs again from the start.** The earlier `elicit()` calls return the answers at once.
- An earlier client: the tool waits while the client asks the user. This needs a session, so use the default mode and one process.

!!! warning
    For a 2026-07-28 client, the code before an `elicit()` runs one time for each answer.
    Put side effects after the last `elicit()`, or check `ctx.is_replay`.
    When the order of the `elicit()` calls can change, give each call a fixed `key=`.

Bolt signs the state of the call with `SECRET_KEY`, and binds it to the tool, the arguments and the caller.
A changed state gets an error.

`ctx.sample()` asks the LLM of the client for a completion.
The 2026-07-28 revision removed it, so it works only with an earlier client that supports sampling.
Otherwise it raises `McpClientError`, and the client gets the message as a tool error.

## Add middleware

MCP calls do not go through Bolt or Django HTTP middleware.
Use MCP middleware for logging, metrics and tracing of each call:

```python
import logging
import time

from bolt_mcp import ToolCall

logger = logging.getLogger("blog.mcp")


@mcp.tool_middleware
async def log_tool_calls(call: ToolCall, call_next) -> dict:
    started = time.perf_counter()
    result = await call_next()
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("tools/call %s error=%s %.1f ms", call.name, result["isError"], elapsed_ms)
    return result
```

A `ToolCall` has these attributes:

- `name`: the tool name.
- `arguments`: the arguments as the client sent them, before the check.
- `meta`: the `_meta` of the request, for example with `traceparent`. The client sets it, so do not trust it.
- `principal`: the same dict as `principal(request)`.

`call_next()` runs the tool and returns the result dict. A tool exception is already an `isError` result there.
The middleware returns that dict or another dict.
An exception in the middleware gets the same treatment as a tool exception.

Bolt refuses a call to an unknown tool, or a call that fails authentication or the guards, before the middleware runs.
The first middleware that you register runs outermost.

`@mcp.resource_middleware` runs around each `resources/read`, and gets a `ResourceRead` with `uri`, `meta` and `principal`.
`@mcp.prompt_middleware` runs around each `prompts/get`, and gets a `PromptGet` with `name`, `arguments`, `meta` and `principal`.
To refuse a read or a prompt, raise `JsonRpcError`:

```python
from bolt_mcp import JsonRpcError, ResourceRead


@mcp.resource_middleware
async def staff_only_drafts(read: ResourceRead, call_next) -> dict:
    if read.uri.startswith("drafts://") and not read.principal.get("is_staff"):
        raise JsonRpcError("Resource not found", kind="resource_not_found")
    return await call_next()
```

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Resource not found"}}
```

`kind="invalid_params"` (the default) gives code -32602.
`kind="resource_not_found"` gives -32002 to an earlier client and -32602 to a 2026-07-28 client.
Bolt does not hide the message of a `JsonRpcError`.

Register each middleware before `mount_mcp()`. A later registration raises `RuntimeError`.
A middleware must be an `async def` function, else Bolt raises `TypeError`.

## Expose existing routes as tools

Give the handlers of existing routes to `expose`:

```python
@api.get("/articles/{article_id}")
async def read_article(article_id: int) -> dict:
    """Read one article."""
    return ARTICLES[article_id]


api.mount_mcp(mcp, expose=[read_article])
```

The tool name is the function name, and the description is the docstring.
The path, query, header and cookie parameters, and the body, become the tool arguments.

!!! warning
    An exposed route runs without its `auth`, its guards and its middleware.
    A client of the MCP endpoint can call it with the auth of the mount only.
    Do not expose a route that needs a guard. Write an `@mcp.tool` with `guards=` for it.

`mount_mcp()` exposes only the routes that you name.
A handler that is not a route of the API, a handler with `File` or `Form` parameters, or a duplicate tool name raises `ValueError`.

To change the name or the description, use `@expose_as_tool`:

```python
from bolt_mcp import expose_as_tool


@api.get("/articles/{article_id}/comments")
@expose_as_tool(name="list_comments", description="List the comments of an article.")
async def article_comments(article_id: int) -> list[dict]:
    return []
```

`expose_routes(mcp, api, include=["/articles/*"], methods=("GET",))` exposes the routes with `@expose_as_tool` that match the patterns.
Call it before `mount_mcp()`. `only_marked=False` also exposes the routes without the decorator.

## Require authentication

### Use Bolt authentication

Give `auth` and `guards` to `mount_mcp()`, as for a route:

```python
from django_bolt.auth import IsAuthenticated, JWTAuthentication, Requires

private = MCP("blog-admin")


@private.tool(guards=[Requires("is_staff", True)])
async def delete_article(article_id: int) -> dict:
    """Delete an article. Staff only."""
    return {"deleted": article_id}


api.mount_mcp(private, "/mcp-admin", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
```

- `auth` reads the credential. It does not refuse a request with no credential.
- `guards` of the mount refuse a request before the protocol runs. A request with no valid token gets `401`.
- `guards` of a tool hide the tool in `tools/list` for a caller that fails them. A call gets the error result `Permission denied for tool 'delete_article'`.

With `auth` and no mount `guards`, one endpoint serves public and private tools.
An anonymous client sees only the tools that it can call.

A mount with no `auth` uses `BOLT_AUTHENTICATION_CLASSES`.
`SessionAuthentication` does not work on an MCP mount, because Django middleware does not run there.

### Verify tokens from your identity provider

Use `ProtectedResource` when an OAuth 2.1 authorization server, for example Auth0 or Keycloak, issues the tokens:

```python
from bolt_mcp import ProtectedResource


def verify_token(token: str) -> dict | None:
    # Check the signature, the audience and the expiry with your provider.
    return None


api.mount_mcp(
    mcp,
    oauth=ProtectedResource(
        resource_url="https://blog.example.com/mcp",
        authorization_servers=["https://login.example.com"],
        required_scopes=("articles:read",),
        token_verifier=verify_token,
    ),
)
```

`token_verifier` is a `def` function. It gets the bearer token and returns the claims as a dict, or `None` to refuse the token.
Bolt reads `sub`, `is_staff`, `is_superuser` and `permissions` from the claims for the guards.
With `required_scopes`, the `scope` or `scp` claim must have each scope.

Bolt serves the metadata of [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) at `/.well-known/oauth-protected-resource/mcp`.
A request with no valid token gets `401` with a `WWW-Authenticate` header that points to it.
A client then finds the authorization server and starts the OAuth flow.

With `oauth`, Bolt ignores the `guards` of the mount. Use tool guards.

### Run the built-in authorization server

`AuthorizationServer` makes your Django project the OAuth 2.1 authorization server.
A client, for example Claude.ai or Claude Code, then signs in the user one time and refreshes the token itself.
The user signs in with a Django username and password.

Add the app to the settings and create its tables:

```python title="settings.py"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django_bolt",
    "bolt_mcp.oauth",
]
```

```bash
python manage.py migrate
```

Mount the server with it:

```python
from bolt_mcp import AuthorizationServer

api.mount_mcp(mcp, oauth=AuthorizationServer(issuer="https://blog.example.com"))
```

Each request to `/mcp` then needs a token:

```bash
curl -i -X POST localhost:8000/mcp
```

```text
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer resource_metadata="https://blog.example.com/.well-known/oauth-protected-resource/mcp"

{"detail":"Unauthorized"}
```

!!! warning
    `issuer` must be the exact public origin of the server, for example `https://blog.example.com`.
    Clients compare it byte by byte, and each token holds it.
    Without `issuer`, the default is `http://localhost:8000`, and Python shows a warning.

The server adds these routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414) |
| `GET` | `/.well-known/oauth-protected-resource/mcp` | Resource metadata (RFC 9728) |
| `POST` | `/oauth/register` | Dynamic client registration (RFC 7591) |
| `GET`, `POST` | `/oauth/authorize` | Sign-in and consent pages |
| `POST` | `/oauth/token` | Gives access tokens and refresh tokens |
| `POST` | `/oauth/revoke` | Revokes a token (RFC 7009) |

The access tokens are HS256 JWTs, signed with `SECRET_KEY`.
They have the claims that Bolt auth reads, so tool guards work as with `JWTAuthentication`.
The server requires PKCE with S256, and it accepts only the exact redirect URIs of a client.
It stores codes and refresh tokens as SHA-256 hashes. A reused refresh token revokes its chain.

Set the options as keyword arguments, or as class attributes of a subclass:

| Option | Default | Effect |
| --- | --- | --- |
| `issuer` | `None` (`http://localhost:8000`) | The public origin. The `iss` claim and the base of each URL. |
| `resource_url` | `issuer` + mount path | The resource and the `aud` claim of the tokens. |
| `scopes_supported` | `("mcp",)` | The scopes in the metadata. |
| `required_scopes` | `()` | The scopes that a token needs for `/mcp`. |
| `access_token_ttl` | `3600` | Seconds that an access token is valid. |
| `refresh_token_ttl` | `2592000` | Seconds that a refresh token is valid (30 days). |
| `auth_code_ttl` | `300` | Seconds that an authorization code is valid. |
| `jwt_secret` | `None` (`SECRET_KEY`) | The signing key. |
| `jwt_algorithm` | `"HS256"` | The signing algorithm. |
| `auto_consent` | `False` | Skip the consent page right after the user signs in. |
| `allow_dynamic_registration` | `True` | Serve `/oauth/register`. |
| `endpoint_prefix` | `"/oauth"` | The prefix of the authorize, token, register and revoke routes. |

Override a method in a subclass to change the behavior:

```python
class BlogAuthorizationServer(AuthorizationServer):
    issuer = "https://blog.example.com"
    access_token_ttl = 1800

    def get_extra_claims(self, user, *, scopes: list[str], client_id: str) -> dict:
        return {"username": user.get_username()}


api.mount_mcp(mcp, oauth=BlogAuthorizationServer())
```

| Method | Bolt calls it | Override it to |
| --- | --- | --- |
| `get_extra_claims(user, *, scopes, client_id)` | When it makes a token | Add claims, for example a tenant or a role |
| `async authenticate(username, password)` | When the user signs in | Use another user store, or add a second factor |
| `async resolve_user(request)` | On each `/oauth/authorize` | Find the signed-in user in another way |
| `async load_user(user_id)` | When it makes a token | Load the user in another way |
| `render_login(params, *, error=None)` | When no user is signed in | Show your own sign-in page |
| `render_consent(params, *, client_name, username)` | When a user is signed in | Show your own consent page |
| `redirect_uri_allowed(registered, redirect_uri)` | On `/oauth/authorize` | Change how redirect URIs match |
| `code_redirect_response(redirect_url)` | After consent | Change how the code goes to the client |

A client that runs on a server, for example Claude.ai, cannot reach `localhost`.
For a test, use a public HTTPS tunnel, and set `issuer` to its URL.

## Deploy the server

A client of the 2026-07-28 revision sends each request with all its state.
Bolt serves it in each mode and in each process.
An earlier client uses a session, and the `MCP()` options set how Bolt serves it:

| Mode | Option | Sessions | Live `elicit()` and `sample()` | More than one process |
| --- | --- | --- | --- | --- |
| Stateful (default) | `MCP("blog")` | Yes | Yes | Only with sticky sessions |
| JSON response | `MCP("blog", json_response=True)` | No | No | Yes |
| Stateless | `MCP("blog", stateless=True)` | No | No | Yes |

- **Stateful** keeps each session in the memory of one process. Run `runbolt --processes 1`, or a load balancer that sends a session to the same process.
- **JSON response** answers a request with one `application/json` object. A tool that sends notifications still gets an event stream.
- **Stateless** answers each request with an event stream, and has no `GET` stream.

Set the public host names of the server with `allowed_hosts`.
The check protects a local server from DNS rebinding:

```python
api.mount_mcp(mcp, allowed_hosts=["blog.example.com"])
```

A host without a port matches each port.
Another `Host` gets `403` with `Forbidden: Host header is not allowed`.
`allowed_hosts=[]` turns off the check. Use it only behind a proxy that checks the host.
`allowed_origins=["https://app.example.com"]` also checks the `Origin` header of a browser request.

`mount_mcp()` needs `SECRET_KEY`. `BOLT_MAX_UPLOAD_SIZE` limits the request body.

`MCP()` takes these arguments:

| Argument | Default | Effect |
| --- | --- | --- |
| `name` | `"django-bolt"` | The server name in `serverInfo`. |
| `version` | `"0.1.0"` | The server version in `serverInfo`. |
| `title`, `website_url`, `icons` | `None` | More data in `serverInfo`. |
| `instructions` | `None` | Text that tells the model how to use the server. |
| `stateless` | `False` | See the modes above. |
| `json_response` | `False` | See the modes above. |
| `list_ttl_ms` | `0` | The `ttlMs` cache hint on each list result. `0` asks for no cache. |
| `list_cache_scope` | `"private"` | The `cacheScope` hint: `"private"` or `"public"`. |
| `mask_error_details` | `True` | Hide the text of an exception from the client. |

`api.mount_mcp(mcp, path="/mcp", *, auth=None, guards=None, oauth=None, expose=None, allowed_hosts=None, allowed_origins=None)` mounts the server.
`bolt_mcp.mount_mcp(api, mcp, ...)` does the same.
A second server at the same path raises `ValueError`.

## Test the server

`TestClient` sends requests through the MCP endpoint in the same process.
It allows its own host, so the host check passes.
Send requests of the 2026-07-28 revision. Each request is complete, so a test needs no session:

```python title="blog/tests.py"
import json

from django_bolt.testing import TestClient

from blog.api import api

MCP_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def call_tool(client: TestClient, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "_meta": MCP_META},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
        },
    )
    assert response.status_code == 200
    events = [line[len("data: ") :] for line in response.text.splitlines() if line.startswith("data: ")]
    return json.loads(events[-1])["result"]


def test_get_article():
    with TestClient(api) as client:
        result = call_tool(client, "get_article", {"article_id": 1})
    assert result["isError"] is False
    assert result["structuredContent"] == {"id": 1, "title": "Hello Bolt", "published": True}
```

The `Mcp-Method` and `Mcp-Name` headers must match the body.
For `resources/read`, `Mcp-Name` is the URI. For `prompts/get`, it is the prompt name.
To test a guarded tool, add an `Authorization` header with a token.

## Supported MCP methods

Bolt serves `server/discover`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `resources/templates/list`, `prompts/list`, `prompts/get` and `subscriptions/listen`.
For an earlier client, it also serves `initialize`, `ping`, and the `GET` and `DELETE` requests of a session.
It speaks the 2026-07-28 revision, and each revision back to 2024-11-05.

Bolt does not serve `completion/complete`, the tasks extension or list-changed notifications.
The lists of tools, resources and prompts do not change after the mount.

## See also

- [Authentication](authentication.md)
- [Permissions](permissions.md)
- [Server-Sent Events and streaming](sse.md)
- [Deployment](deployment.md)
- [Testing](testing.md)
- [MCP specification](https://modelcontextprotocol.io/specification)

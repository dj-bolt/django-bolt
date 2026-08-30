# django-bolt example project

A Django project exercising django-bolt features: REST APIs (`core`, `users`,
`missions`), middleware demos, and an MCP server (`mcp_demo`) built with
bolt-mcp. `nanodjango_helloworld.py` is a standalone single-file app
(`uv run python/example/nanodjango_helloworld.py runbolt`).

## Setup

```bash
python manage.py migrate            # includes the bolt_mcp.oauth tables
python manage.py runbolt --dev --host 127.0.0.1 --port 8001
```

`--dev` runs one process with auto-reload. Set `DEBUG = True` in `testproject/settings.py`
to turn on Django Debug Toolbar, which needs one process. See
[docs/src/topics/debug-toolbar.md](../../docs/src/topics/debug-toolbar.md).

Port 8001 matters for the MCP demo: the OAuth issuer in `mcp_demo/api.py` is
pinned to `http://127.0.0.1:8001`.

## MCP demo (`mcp_demo/api.py`)

An MCP server served over Streamable HTTP at `/mcp`, protected by the built-in
OAuth 2.1 Authorization Server. It shows native tools, Django async ORM tools,
progress streaming, resources + resource templates, prompts, an exposed REST
route, sampling/elicitation, and Rust-evaluated per-tool guards.

Create a user to sign in as (staff ⇒ gets the `reports:read` permission via
`get_extra_claims`, unlocking the `read_report` tool):

```bash
python manage.py shell -c "
from django.contrib.auth import get_user_model
u, _ = get_user_model().objects.get_or_create(username='demo')
u.is_staff = True; u.set_password('demo12345'); u.save()"
```

### Headless end-to-end check

`mcp_oauth_client_demo.py` walks the whole flow a real MCP client performs —
discovery → dynamic client registration → PKCE login → consent → token →
`initialize`/`tools/list`/`tools/call` — with no browser and no dependencies:

```bash
python mcp_oauth_client_demo.py     # defaults: --username demo --password demo12345
```

It prints the issued access token at the end.

### Connect Claude Code

Use the token printed by the demo script:

```bash
claude mcp add --transport http bolt-example http://127.0.0.1:8001/mcp \
    --header "Authorization: Bearer <token>"
claude    # the agent now sees add, count_users, crunch, whoami, read_report, ...
```

Or let Claude Code do the OAuth flow itself (it supports dynamic client
registration): add the server without the header, then run `/mcp` inside Claude
Code and sign in as `demo` in the browser window it opens.

Other MCP clients work the same way, e.g. the inspector:

```bash
npx @modelcontextprotocol/inspector   # Streamable HTTP → http://127.0.0.1:8001/mcp
```

Note: `/mcp` is a JSON-RPC POST endpoint — opening it in a browser issues a GET
(reserved for the SSE channel) and returns an error.

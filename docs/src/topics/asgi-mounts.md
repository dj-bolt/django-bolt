---
icon: lucide/plug
---

# ASGI Mounts

This guide explains how to mount HTTP ASGI applications inside Django-Bolt.

Use this when you want to serve existing Django URLconf apps (for example admin, allauth, or legacy ASGI apps) under a path prefix, while keeping Bolt routes fast.

## Overview

Django-Bolt provides two mount APIs:

- `api.mount_asgi(path, app)`: Mount any HTTP ASGI callable.
- `api.mount_django(path, app=None, *, clear_root_path=False)`: Mount Django's ASGI app (or a provided ASGI app).

These mounts are HTTP-only (no WebSocket/lifespan support in this mount bridge).

## `mount_asgi()`

Mount a generic ASGI app under a static prefix:

```python
from django_bolt import BoltAPI

api = BoltAPI()

async def metrics_app(scope, receive, send):
    assert scope["type"] == "http"

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})

api.mount_asgi("/metrics", metrics_app)
```

### Scope behavior

For `api.mount_asgi("/metrics", app)`:

- `GET /metrics`
  - `scope["root_path"] == "/metrics"`
  - `scope["path"] == "/"`
- `GET /metrics/v1/ping`
  - `scope["root_path"] == "/metrics"`
  - `scope["path"] == "/v1/ping"`

Headers and query strings are forwarded in the ASGI scope.

## `mount_django()`

Mount Django's ASGI app under a prefix:

```python
from django_bolt import BoltAPI

api = BoltAPI()
api.mount_django("/django")
```

By default, this calls `django.core.asgi.get_asgi_application()`.

You can also pass your own ASGI app:

```python
api.mount_django("/legacy", app=custom_asgi_app)
```

### `clear_root_path` behavior

`mount_django()` can clear `scope["root_path"]` before passing control to Django:

```python
api.mount_django("/admin", clear_root_path=True)
```

Use `clear_root_path=True` when the mounted Django app's URL patterns already include the mount prefix.

- Example: admin URL patterns are `/admin/...` and you mount at `/admin`.
  - With `clear_root_path=True`, Django sees full paths like `/admin/login/`.
- If Django URL patterns are relative to the mount root (for example `/login/` under the mount), keep the default `clear_root_path=False`.

## Rules and constraints

- Mount paths must be static (no dynamic segments like `/{id}`).
- Prefixes are normalized (leading slash, no trailing slash except `/`).
- Duplicate exact mount prefixes are rejected.
- Exact collisions between Bolt route paths and mount prefixes are rejected.
- Non-exact prefix overlap is allowed.
- Longest prefix wins when multiple mounts match.

## Routing precedence

Request dispatch order:

1. Bolt route matching
2. ASGI mount fallback (only on route miss)

This means mounted apps do not override matching Bolt routes.

Additionally, API near-miss behavior (such as method mismatch and trailing-slash redirect checks) is resolved before mount fallback, so ASGI mounts do not hijack those API semantics.

## Middleware boundary

ASGI mounts run outside Bolt's route middleware pipeline.

That means Bolt-level middleware features (for example rate limits, Bolt auth guards, and Bolt CORS behavior) do not apply to mounted ASGI apps. Mounted Django apps should rely on Django middleware/settings for those concerns.

## Timeouts and body limits

- `BOLT_ASGI_MOUNT_TIMEOUT` (seconds, default `30`) limits how long Bolt waits for the mounted ASGI app to send the first `http.response.start` message (response headers). Once headers arrive, the response body streams indefinitely with no additional timeout. Timeout returns `504 Gateway Timeout`.
- `BOLT_MAX_UPLOAD_SIZE` also applies to ASGI mount request bodies. Oversized bodies return `413 Payload Too Large`.
- Request-body buffering for mounts currently has no dedicated read timeout before handoff to the mounted ASGI app. Enforce slow-client/read timeouts at your reverse proxy or ingress.

## Testing

`TestClient` and `AsyncTestClient` use the same mount conflict validation and mount dispatch behavior as production startup.

## Performance note

Matched Bolt API endpoints keep their existing fast path.

ASGI mount lookup runs on route-miss flow, so normal matched API routes do not pay mount fallback overhead.

---
description: Serve Django views, the Django admin or another ASGI application under a path prefix, next to your Bolt routes.
---

# ASGI mounts

An ASGI mount sends each request under a path prefix to an ASGI application.
Use it to serve Django views from your URLconf, or an app of another ASGI framework, on the same port as your Bolt routes.

Mount the Django URLconf at `/legacy`:

```python title="blog/urls.py"
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import path


def article_detail(request, pk):
    return JsonResponse({"id": pk, "path": request.path, "path_info": request.path_info})


def old_article(request, pk):
    return redirect(f"/articles/{pk}/")


urlpatterns = [
    path("articles/<int:pk>/", article_detail),
    path("old/<int:pk>/", old_article),
]
```

```python title="blog/api.py"
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/legacy/health")
async def health() -> dict:
    return {"status": "ok"}


api.mount_django("/legacy")
```

```bash
curl localhost:8000/legacy/articles/3/
```

```json
{"id": 3, "path": "/legacy/articles/3/", "path_info": "/articles/3/"}
```

```bash
curl localhost:8000/legacy/health
```

```json
{"status": "ok"}
```

`/legacy/health` matches a Bolt route, so Bolt serves it.
Each other path under `/legacy` goes to Django.

## Mount Django views

`api.mount_django(path, app=None, *, clear_root_path=False)` mounts Django's ASGI application.
The default app is `django.core.asgi.get_asgi_application()`.
Django runs its own `MIDDLEWARE` for each request, as under any ASGI server.

By default, Django gets the prefix as the script name.
It resolves `path_info`, the path after the prefix, against `ROOT_URLCONF`.
Thus `urlpatterns` in the example have no `legacy/` part, and `reverse()` adds the prefix.

A redirect to a path that starts with `/` gets the prefix.
In the example, `/legacy/old/3/` redirects to `/legacy/articles/3/`.

With `FORCE_SCRIPT_NAME`, Django takes the setting as the script name and ignores the prefix.
Then the URL patterns must contain the prefix, as in the next section, and Bolt does not change a redirect.
See [Serve under a sub-path](deployment.md#serve-under-a-sub-path).

### Keep the prefix in the URL patterns

Set `clear_root_path=True` when the URL patterns already contain the prefix:

```python title="reports/urls.py"
from django.http import JsonResponse
from django.urls import path


def summary(request):
    return JsonResponse({"path": request.path, "path_info": request.path_info})


urlpatterns = [path("reports/summary/", summary)]
```

```python
api.mount_django("/reports", clear_root_path=True)
```

```bash
curl localhost:8000/reports/summary/
```

```json
{"path": "/reports/summary/", "path_info": "/reports/summary/"}
```

### Send all other paths to Django

Mount Django at `/` to serve each path that has no Bolt route:

```python
api.mount_django("/", clear_root_path=True)
```

## Serve the Django admin

`runbolt` mounts the Django admin for you.
It finds the prefix of the admin site in `ROOT_URLCONF`, for example `/admin`, and calls `mount_django()` for it.
To turn this off, run `python manage.py runbolt --no-admin`.

An admin site in `i18n_patterns()` has one prefix for each language of `settings.LANGUAGES`, for example `/en/admin` and `/fr/admin`.
`runbolt` mounts each prefix. Set `LANGUAGES` to the languages that you use, because each language adds a mount.

An admin prefix with a path parameter, for example `path("<tenant>/admin/", site.urls)`, is not a static path.
A mount path must be static, so `runbolt` mounts Django at the root path for it, and prints a warning.
Bolt routes answer first. Django answers each path that has no Bolt route, so `/acme/admin/` goes to the admin.
An admin site at the root path, `path("", admin.site.urls)`, gets the same mount and the same warning.

This mount costs nothing for a request to a Bolt route, because Bolt reads the mounts only when no route matches.
Each path that has no Bolt route gets the answer of Django, for example a Django 404 page.
A path of a Bolt route with another method still gets 405 from Bolt.

If you call `api.mount_django("/", ...)` yourself, `runbolt` keeps your mount and does not add one.

## Mount another ASGI app

`api.mount_asgi(path, app)` mounts any ASGI 3 callable, for example a Starlette or FastAPI app:

```python
import json


async def status_app(scope, receive, send):
    body = json.dumps({"root_path": scope["root_path"], "path": scope["path"]}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


api.mount_asgi("/status", status_app)
```

```bash
curl localhost:8000/status/v1/ping
```

```json
{"root_path": "/status", "path": "/v1/ping"}
```

For an HTTP request, the scope has these values:

| Key | Value |
| --- | --- |
| `root_path` | The mount prefix, for example `/status` |
| `path` | The path after the prefix, percent-decoded. `/status` gives `/`. |
| `raw_path` | The request path as received, with the prefix and the `%XX` escapes |
| `query_string` | The query string as bytes, without `?` |
| `headers` | The request headers as a list of byte pairs |

`mount_django()` puts the prefix back in `path` before Django reads the scope.
With `FORCE_SCRIPT_NAME`, `root_path` starts with the script name, and `reverse()` in the app gives the URLs of the project under the script name.

Bolt does not send lifespan events to a mounted app.
Thus the startup and shutdown handlers of a Starlette or FastAPI app do not run.
Use the [lifespan](lifespan.md) of the `BoltAPI` for that work.

## Serve WebSocket connections from a mount

A mounted app also gets WebSocket connections.
Bolt first tries its own WebSocket routes. A connection that matches none goes to the mount with the longest matching prefix.

```python
async def echo_app(scope, receive, send):
    await receive()  # websocket.connect
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        await send({"type": "websocket.send", "text": message["text"]})


api.mount_asgi("/ws", echo_app)
```

A connection to `/ws/room` gets each text message back.

The WebSocket scope differs from the HTTP scope.
`path` keeps the mount prefix, for example `/ws/room`, and `root_path` is `/ws`.

The handshake follows the app:

- `websocket.accept` sends the `101` response. Its `subprotocol` and `headers` go in that response.
- `websocket.close` before the accept, or a return before the accept, refuses the handshake with `403`.
- An exception before the accept gives `500`.

`mount_django()` with Django's own ASGI app gets no WebSocket connections, because that app serves only HTTP.
`mount_django()` with a custom app, for example a Channels `ProtocolTypeRouter`, gets them.
Bolt does not run `auth` or guards for a mounted app. Check the user in the app.

## What runs for a mounted app

Bolt sends a request to a mount only when no Bolt route matches the path.
Thus a mount cannot hide a Bolt route.
A path of a Bolt route with another method gets `405` from Bolt. Thus in the first example, `POST /legacy/health` does not go to Django.

For a request to a mount:

- The global CORS settings and compression apply.
- The `auth`, guards, `@rate_limit`, `@cors` and the middleware of a Bolt route do not run.
- `BOLT_MAX_UPLOAD_SIZE` applies to the body. A larger body gets `413`.
- The app must send its response headers within `BOLT_ASGI_MOUNT_TIMEOUT` seconds. The default is 30. After that, the client gets `504`. The body can then take any time.
- When the client leaves, the app gets `http.disconnect` from `receive()`. A later `send()` does nothing.

## Rules for the mount path

- The path is static. A path parameter, as in `/items/{id}`, raises `ValueError`.
- Bolt adds a leading slash and removes a trailing slash. `/` stays `/`.
- The `prefix` of the `BoltAPI` comes before the mount path.
- Two mounts at the same path raise `ValueError`.
- A mount at the exact path of a Bolt route stops startup with `ValueError`. A Bolt route under the mount path, as `/legacy/health` above, is valid.
- A mount at `/` and a Bolt route at `/` can exist together. The route answers `/`, and the mount gets each request that no route matches.
- When two mounts match, the longer prefix wins.

## Test a mounted app

`TestClient` sends requests to a mount as `runbolt` does. It also checks the mount paths at startup:

```python
from django_bolt.testing import TestClient


def test_status_app():
    with TestClient(api) as client:
        response = client.get("/status/v1/ping")
    assert response.json() == {"root_path": "/status", "path": "/v1/ping"}
```

`WebSocketTestClient` connects to a mounted WebSocket app.
See [Testing](testing.md).

## See also

- [Routing](routing.md)
- [WebSocket](websocket.md)
- [Middleware](middleware.md)
- [BoltAPI reference](../ref/api.md)
- [runbolt reference](../ref/runbolt.md)

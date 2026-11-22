---
description: Send Django's request_started and request_finished signals for Django-Bolt requests with the BOLT_EMIT_SIGNALS setting.
---

# Signals

By default, Django-Bolt does not send Django's request signals. Set `BOLT_EMIT_SIGNALS = True` to send `request_started` and `request_finished` for each request that reaches a handler. Use it when a receiver of these signals must see Bolt requests, for example an audit log.

The other Django signals do not need this setting. The ORM sends `pre_save`, `post_save` and the other model signals, and `django.contrib.auth` sends its signals, as in Django.

## Turn on the request signals

Add the setting:

```python title="mysite/settings.py"
BOLT_EMIT_SIGNALS = True
```

Connect receivers as in Django:

```python title="audit/api.py"
from django.core.signals import request_finished, request_started
from django.dispatch import receiver

from django_bolt import BoltAPI

api = BoltAPI()


@receiver(request_started)
def on_request_started(sender, scope, **kwargs):
    print(f"started: {scope['method']} {scope['path']} ({sender.__name__})")


@receiver(request_finished)
def on_request_finished(sender, **kwargs):
    print("finished")


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    print(f"handler: article {article_id}")
    return {"id": article_id}
```

```bash
curl localhost:8000/articles/3
```

The output of `runbolt`:

```text
started: GET /articles/3 (BoltAPI)
handler: article 3
finished
```

The `sender` of the two signals is the `BoltAPI` class.

Bolt reads `BOLT_EMIT_SIGNALS` when you create a `BoltAPI`. In a test, change the setting before you create the API:

<!-- fragment -->
```python
with override_settings(BOLT_EMIT_SIGNALS=True):
    api = BoltAPI()
```

## Read the request in `request_started`

A receiver of `request_started` gets a `scope` dict:

| Key | Value |
| --- | --- |
| `type` | `"http"` |
| `method` | The HTTP method, for example `"GET"`. |
| `path` | The request path, for example `"/articles/3"`. |
| `query_string` | The query string as `bytes`, still encoded, for example `b"a=1&b=x%20y"`. It is `b""` for a request with no query. |
| `headers` | A `dict` of the request headers. Each name is in lower case. |

`request_finished` has no `scope`.

## Know which requests send the signals

Bolt sends the signals around the Python part of a request. A request that Bolt answers in Rust sends no signal.
A 404, a 405, a 401 or a 403 of Rust goes through Python when the API has Python or Django middleware. See [Responses from Rust](middleware.md#responses-from-rust).

| Request | Signals |
| --- | --- |
| The handler returns a response. | Both |
| The handler raises an error, for example `HTTPException` or an unexpected error that gives 500. | Both |
| The request body fails validation (422). | Both |
| No route matches the path (404), or the path has no route for the method (405). | Both when the API has middleware. Else none. |
| Authentication or a guard refuses the request (401, 403). | Both when the API has middleware. Else none. |
| A rate limit refuses the request (429). | None |
| A CORS preflight, or an automatic `OPTIONS` response. | None |
| A path or query value has the wrong type (422). | None |
| A static file or a media file. | None |
| A WebSocket connection. | None |

An app that you mount with `api.mount_django()`, for example the Django admin, is a Django ASGI application. Django sends both signals for its requests, with the sender `ASGIHandler`. This does not depend on `BOLT_EMIT_SIGNALS`.

Bolt never sends `got_request_exception`.

## Know when the signals run

Bolt sends the two signals with `Signal.asend()`. A sync receiver runs in a thread of the Bolt ORM pool, not in the thread of the handler. Thus `threading.local` data of the handler is not visible in a receiver.

`request_started` runs before the handler. `request_finished` runs when the handler returns. For a streaming response, `request_finished` runs before Bolt sends the body. The generator of the body runs after the signal.

## Know the cost

With the signals on, each request awaits the two signal sends. Bolt also cannot use its fast path, which runs a handler with no event loop. Thus each request goes through the event loop. Turn the setting on only when a receiver needs it.

You do not need the signals for the database connections. Bolt manages the connections of its threads itself. See [Configure database connections](deployment.md#configure-database-connections).

## See also

- [Settings reference](../ref/settings.md): `BOLT_EMIT_SIGNALS`.
- [Lifespan](lifespan.md): run code when the server starts and stops.
- [Middleware](middleware.md): run code around each request.
- [Django signals](https://docs.djangoproject.com/en/stable/topics/signals/)

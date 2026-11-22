---
description: Reference for the Request object - each attribute and method, with its type and behavior.
---

# Request

A handler gets the request object when it has an argument named `request` or `req`.
For a task-based introduction, see [Use the Request object](../topics/requests.md#use-the-request-object).

```python
from django_bolt import Request
```

`Request` is a type for your editor and type checker. At runtime, the handler gets an object of the Rust class `PyRequest`.
Do not use `isinstance(request, Request)`. It is `False` when the request has no `session`.

**Example**

```python
from django_bolt import BoltAPI, Request

api = BoltAPI()


@api.get("/articles/{article_id}/debug")
async def debug_article(request: Request, article_id: int) -> dict:
    return {
        "method": request.method,
        "path": request.path,
        "params": request.params,
        "query": request.query,
        "full_path": request.get_full_path(),
    }
```

```bash
curl "localhost:8000/articles/3/debug?preview=1"
```

```json
{"method": "GET", "path": "/articles/3/debug", "params": {"article_id": 3}, "query": {"preview": "1"}, "full_path": "/articles/3/debug?preview=1"}
```

## Attributes

All attributes are read-only, except `user` and `auser`.

| Attribute | Type | Description |
| --- | --- | --- |
| `method` | `str` | The HTTP method, for example `"GET"`. |
| `path` | `str` | The path, with no query string. |
| `body` | `bytes` | The raw body. |
| `headers` | `dict[str, Any]` | The headers, with lower-case names. A typed `Header()` argument replaces its value with the converted value. |
| `cookies` | `dict[str, Any]` | The cookies. A typed `Cookie()` argument replaces its value with the converted value. |
| `raw_cookies` | `dict[str, str]` | The cookies as they arrived, as strings. Each read gives a new `dict`. |
| `query` | `dict[str, Any]` | The query values. A repeated key keeps its last value. A typed query argument replaces its value with the converted value. |
| `params` | `dict[str, Any]` | The path parameters, converted to the types of the handler arguments. |
| `form` | `dict[str, Any]` | The form fields. It is empty unless the handler has a `Form()` or `File()` argument. |
| `files` | `dict[str, Any]` | The uploaded files. It is empty unless the handler has a `Form()` or `File()` argument. Use an `UploadFile` argument in place of this dict. |
| `META` | `dict[str, str]` | A Django `HttpRequest.META` dict. It has `REQUEST_METHOD`, `PATH_INFO`, `QUERY_STRING`, `SERVER_NAME`, `SERVER_PORT`, `SERVER_PROTOCOL`, `REMOTE_ADDR`, `REMOTE_HOST`, `SCRIPT_NAME`, `CONTENT_TYPE`, `CONTENT_LENGTH`, and `HTTP_*` for each header. Bolt builds it on the first read. |
| `state` | `dict[str, Any]` | A dict for data of your own during the request. Middleware can put data in it. |
| `context` | `dict \| None` | The data of the authentication backend, for example `user_id` and the JWT claims. `None` on a route with no authentication. |
| `user` | `Any` | The user that the authentication backend found, or `None`. In an async handler, the first read runs a blocking database query. Django's `AuthenticationMiddleware` can set it. |
| `auser` | `Callable` | An async function that returns the user. It returns `AnonymousUser` when no user is authenticated. Django's `alogin()` and `alogout()` can set it. |
| `scheme` | `str` | `"http"` or `"https"`. It is `"https"` only when a trusted proxy says so. `SECURE_PROXY_SSL_HEADER` applies, as in Django. |
| `session` | `SessionBase` | The Django session. It exists only when Django's `SessionMiddleware` runs on the route. Without it, a read raises `AttributeError`. |

For a handler with only a `request` argument, Bolt reads the source of the handler when you register it. Bolt then reads only the parts of the request that the handler uses.

## Methods

### get_full_path()

<!-- fragment -->
```python
request.get_full_path() -> str
```

Return the path and the query string, for example `"/articles?page=2"`. The query string stays as the client sent it.

### build_absolute_uri()

<!-- fragment -->
```python
request.build_absolute_uri(location: str | None = None) -> str
```

Return the absolute URL of `location`, or of the request when `location` is `None`. Django's `HttpRequest.build_absolute_uri()` builds the URL.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `location` | `str \| None` | `None` | A path or a URL. A relative path joins the URL of the request. An absolute URL comes back with no change. |

**Errors**

- `DisallowedHost`: the host of the request is not in `ALLOWED_HOSTS`. Bolt answers it with a 400 response. An absolute `location` does not check the host.

### is_secure()

<!-- fragment -->
```python
request.is_secure() -> bool
```

Return `True` when `scheme` is `"https"`.

### get()

<!-- fragment -->
```python
request.get(key: str, default: Any = None) -> Any
```

Return an attribute by its name. The keys are `"method"`, `"path"`, `"body"`, `"params"`, `"query"`, `"headers"`, `"cookies"`, `"state"` and `"context"`. `"auth"` is the same as `"context"`.
Each other key returns `default`.

### request[key]

<!-- fragment -->
```python
request[key: str] -> Any
```

Return an attribute by its name, as `get()` does. The keys are the keys of `get()`, except `"auth"`. Another key raises `KeyError`.

### setdefault()

<!-- fragment -->
```python
request.setdefault(key: str, default: Any = None) -> Any
```

Return the same value as `get()`. For `"state"`, make the state dict if it does not exist.

## Other attributes

A read of an attribute that the request does not have returns the value of that name in `state`.
Thus `request.state["tenant"] = tenant` makes `request.tenant` available.
When `state` does not have the name, the read raises `AttributeError`.

You cannot set a new attribute on the request. `request.tenant = tenant` raises `AttributeError`. Use `state`.

## See also

- [Requests](../topics/requests.md)
- [Parameters reference](params.md)
- [Authentication](../topics/authentication.md)
- [Middleware](../topics/middleware.md)

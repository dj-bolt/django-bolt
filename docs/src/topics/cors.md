---
description: Let a browser app on another origin call your API. Configure CORS with Django settings or per route with @cors.
---

# CORS

A browser blocks a page on one origin from reading a response from another origin, unless the response allows it.
Cross-Origin Resource Sharing (CORS) is the set of headers that allow it.
Django-Bolt adds these headers in Rust. It reads the settings of [django-cors-headers](https://github.com/adamchainz/django-cors-headers), so you do not need that package.

## Allow a front end on another origin

Name the origins in `settings.py`:

```python title="settings.py"
CORS_ALLOWED_ORIGINS = ["https://app.example.com"]
```

Each route of the API now answers that origin:

```python title="blog/api.py"
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/articles")
async def list_articles() -> list:
    return [{"id": 1, "title": "Hello"}]


@api.post("/articles")
async def create_article() -> dict:
    return {"id": 2}
```

```bash
curl -i localhost:8000/articles -H "Origin: https://app.example.com"
```

```http
HTTP/1.1 200 OK
access-control-allow-origin: https://app.example.com
vary: Origin
```

An origin that is not in the list gets the response with no CORS headers. The browser then hides the response from the page.

Before a `POST` with a JSON body, the browser sends a preflight `OPTIONS` request. Bolt answers it with a 204:

```bash
curl -i -X OPTIONS localhost:8000/articles \
  -H "Origin: https://app.example.com" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"
```

```http
HTTP/1.1 204 No Content
allow: GET, POST, OPTIONS
access-control-allow-origin: https://app.example.com
access-control-allow-methods: GET, POST, PUT, PATCH, DELETE, OPTIONS, QUERY
access-control-allow-headers: Content-Type, Authorization
access-control-max-age: 3600
vary: Origin, Access-Control-Request-Method, Access-Control-Request-Headers
```

You do not write an `OPTIONS` handler. Bolt answers the preflight of each route path.
Bolt also adds the CORS headers to error responses, such as a 401, a 404 or a 429. The page can then read the error.

## Configure CORS for the whole API

Bolt reads these settings at startup:

| Setting | Default | Effect |
| --- | --- | --- |
| `CORS_ALLOWED_ORIGINS` | `[]` | The origins that Bolt allows, for example `"https://app.example.com"` |
| `CORS_ALLOWED_ORIGIN_REGEXES` | `[]` | Patterns of the origins that Bolt allows |
| `CORS_ALLOW_ALL_ORIGINS` | `False` | Allow each origin with `access-control-allow-origin: *` |
| `CORS_ALLOW_CREDENTIALS` | `False` | Send `access-control-allow-credentials: true` |
| `CORS_ALLOW_METHODS` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `QUERY` | The methods in a preflight answer |
| `CORS_ALLOW_HEADERS` | `Content-Type`, `Authorization` | The request headers in a preflight answer |
| `CORS_EXPOSE_HEADERS` | `[]` | The response headers that the page can read |
| `CORS_PREFLIGHT_MAX_AGE` | `3600` | The seconds that the browser keeps a preflight answer |

With none of `CORS_ALLOWED_ORIGINS`, `CORS_ALLOWED_ORIGIN_REGEXES` and `CORS_ALLOW_ALL_ORIGINS`, Bolt sends no CORS headers.

An origin is the scheme, the host and the port, with no path and no trailing slash: `https://app.example.com`.
A `"*"` in `CORS_ALLOWED_ORIGINS` is not a wildcard. Use `CORS_ALLOW_ALL_ORIGINS = True`.

A pattern in `CORS_ALLOWED_ORIGIN_REGEXES` can match a part of the origin. Start it with `^` and end it with `$`:

```python title="settings.py"
CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://[a-z0-9-]+\.example\.com$"]
```

Patterns use the syntax of the Rust `regex` crate, which has no look-around and no back-references.
An empty pattern, a pattern that does not compile, or a header name that is not valid stops startup with `ImproperlyConfigured`.

## Send cookies or an `Authorization` header

A browser sends cookies to another origin only when the page asks for it (`credentials: "include"` in `fetch`) and the response allows it:

```python title="settings.py"
CORS_ALLOWED_ORIGINS = ["https://app.example.com"]
CORS_ALLOW_CREDENTIALS = True
```

An `Authorization` header with a token is not a credential in this sense. It needs only `Authorization` in `CORS_ALLOW_HEADERS`, which is in the default list.

!!! warning "Do not allow all origins with credentials"
    With `CORS_ALLOW_ALL_ORIGINS = True` and `CORS_ALLOW_CREDENTIALS = True`, Bolt copies the `Origin` of each request into `access-control-allow-origin`.
    Thus each site on the internet can send requests with the cookies of your users and read the responses. Bolt prints a warning at startup.
    List your origins instead.

A cookie backend of Bolt also checks the `Origin` of each unsafe request. CORS does not turn that check off. See [Read the token from a cookie](authentication.md#read-the-token-from-a-cookie).
Thus a front end on another origin, also on a subdomain of your site, cannot send an unsafe request with a cookie token. Serve the front end and the API from one origin, for example through a proxy, or send the token in the `Authorization` header.

## Set CORS for one route

`@cors` gives a route its own allow list. Put it below the route decorator:

```python
from django_bolt import cors


@api.get("/embed/articles/{article_id}")
@cors(origins=["*"])
async def embed_article(article_id: int) -> dict:
    return {"id": article_id, "title": "Hello"}


@api.get("/tenants/stats")
@cors(origin_regexes=[r"^https://[a-z0-9-]+\.example\.com$"], credentials=True, max_age=600)
async def tenant_stats() -> dict:
    return {"articles": 12}
```

| Request | `access-control-allow-origin` |
| --- | --- |
| `GET /embed/articles/1`, `Origin: https://anyone.example` | `*` |
| `GET /tenants/stats`, `Origin: https://acme.example.com` | `https://acme.example.com` |
| `GET /tenants/stats`, `Origin: https://admin.example.org` | none, also when `CORS_ALLOWED_ORIGINS` has it |

The `origins` and `origin_regexes` of `@cors` replace the global lists. They do not add to them.
`@cors` needs `origins` or `origin_regexes`. With neither, it raises `ValueError`.
To use only the global settings, remove the decorator.

`@cors` takes `origins`, `origin_regexes`, `methods`, `headers`, `credentials` and `max_age`.
An option that you do not give gets the default of the table above, not the value of the global setting.
For `expose_headers`, use the dict form: `@middleware(type="cors", origins=[...], expose_headers=["X-Total-Count"])`.

`@cors(origins=["*"], credentials=True)` copies each origin, as the warning above describes. Bolt gives a `RuntimeWarning` for it.

## Set CORS for a group of routes

A `Router` or a `BoltAPI` takes a CORS dict in `middleware`:

```python
from django_bolt import Router

partner = Router(prefix="/partner", middleware=[{"type": "cors", "origins": ["https://partner.example.org"]}])


@partner.get("/articles")
async def partner_articles() -> list:
    return []


api.include_router(partner)
```

The most specific config wins: the route, then the router, then the API, then the settings.
A router dict with no origins and no patterns keeps the allow list of the outer level.

## Turn CORS off for a route

`@skip_middleware("cors")` removes the CORS headers from one route:

```python
from django_bolt import skip_middleware


@api.get("/internal/metrics")
@skip_middleware("cors")
async def internal_metrics() -> dict:
    return {"requests": 0}
```

## Know the limits

- A WebSocket route does not use `@cors`. The handshake checks `Origin` against the global settings only.
  With no CORS settings, Bolt refuses each cross-origin handshake. See [WebSocket](websocket.md).
- A preflight to a path with no route gets a 204 when the global settings allow CORS, and a 404 otherwise.
- `TestClient` reads the CORS settings, so a test can check the headers. See [Testing](testing.md).

## See also

- [Middleware reference](../ref/middleware.md#cors): every argument of `@cors`.
- [Settings reference](../ref/settings.md): the CORS settings.
- [Authentication](authentication.md): cookie tokens and the cross-site check.
- [Middleware](middleware.md): the order of Bolt, Python and Django middleware.

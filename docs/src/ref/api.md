---
description: Reference for BoltAPI and Router - each constructor argument, route decorator, mount method and include method, with types and defaults.
---

# BoltAPI and Router

`BoltAPI` holds the routes of an API. `Router` holds a group of routes that you include in a `BoltAPI`.
For a task-based introduction, see [Routing](../topics/routing.md).

```python
from django_bolt import BoltAPI, Router
```

## BoltAPI

<!-- fragment -->
```python
BoltAPI(
    prefix: str = "",
    trailing_slash: str = "strip",
    namespace: str | None = None,
    middleware: list | None = None,
    django_middleware: bool | list[str] | dict | None = None,
    enable_logging: bool = True,
    logging_config: LoggingConfig | None = None,
    compression: CompressionConfig | bool | None = None,
    openapi_config: OpenAPIConfig | None = None,
    validate_response: bool = True,
    include_in_schema: bool | None = None,
    lifespan: Callable | None = None,
    error_formatter: Callable | None = None,
)
```

Make an API. `runbolt` finds a `BoltAPI` that you assign at the top level of an `api.py` module. See [Find the API modules](../topics/routing.md#find-the-api-modules).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `prefix` | `str` | `""` | A path prefix for each route of the API, for example `"/api/v1"`. Bolt adds a leading slash and removes a trailing slash. |
| `trailing_slash` | `str` | `"strip"` | `"strip"` removes the trailing slash of each path. `"append"` adds one. `"keep"` keeps the path as you wrote it. See [Control trailing slashes](../topics/routing.md#control-trailing-slashes). |
| `namespace` | `str \| None` | `None` | A namespace for `reverse()`. With `"blog"`, a route named `"detail"` reverses as `"blog:detail"`. |
| `middleware` | `list \| None` | `None` | Middleware for each route of the API: middleware classes, `DjangoMiddleware(...)`, `DjangoMiddlewareStack(...)` or middleware configs. An instance of a middleware class raises `TypeError`. See [Middleware](../topics/middleware.md). |
| `django_middleware` | `bool \| list[str] \| dict \| None` | `None` | Run Django middleware on each route. `True` loads `settings.MIDDLEWARE`. A list gives the dotted paths to load, in order. A dict with `"include"` or `"exclude"` lists filters `settings.MIDDLEWARE`. `None` or `False` loads none. |
| `enable_logging` | `bool` | `True` | Use `logging_config`, or the default configuration. `False` removes the exception log and the default `skip_paths`. It does not turn off the access log. See [Logging](../topics/logging.md). |
| `logging_config` | `LoggingConfig \| None` | `None` | The log configuration. `None` uses the default configuration. |
| `compression` | `CompressionConfig \| bool \| None` | `None` | `None` uses `CompressionConfig()`. `False` turns compression off for this API. Another value raises `TypeError`. The `BOLT_COMPRESSION` setting wins over this argument. See [Compression](../topics/compression.md). |
| `openapi_config` | `OpenAPIConfig \| None` | `None` | The OpenAPI configuration. `None` serves the documentation at `/docs`, with the title from the `PROJECT_NAME` or `SITE_NAME` setting, or `"API"`. See [OpenAPI](../topics/openapi.md). |
| `validate_response` | `bool` | `True` | The default of `validate_response` for each route of the API. |
| `include_in_schema` | `bool \| None` | `None` | The default OpenAPI visibility of each route of the API. A route, view or viewset value wins. `None` takes the value of the API that mounts this API, or `True`. |
| `lifespan` | `Callable \| None` | `None` | A function that takes the `BoltAPI` and returns an async context manager. Code before its `yield` runs at startup. Code after it runs at shutdown. See [Lifespan](../topics/lifespan.md). |
| `error_formatter` | `Callable \| None` | `None` | A sync function `(error: BoltError, request) -> Response` that formats each error response of the framework. `None` keeps the `{"detail": ...}` body. See [Error formatters](exceptions.md#error-formatters). |

**Errors**

- `ValueError`: `trailing_slash` is not `"strip"`, `"append"` or `"keep"`, or the `logger_name` of `logging_config` is empty or is not a `str`.
- `TypeError`: `error_formatter` is not callable, or is an async function.

**Attributes**

`prefix`, `trailing_slash`, `namespace` and `include_in_schema` hold the values of the arguments. `prefix` holds the normalized form.

**Example**

```python
from django_bolt import BoltAPI

api = BoltAPI(prefix="/api", trailing_slash="append", namespace="blog")


@api.get("/articles", name="article-list")
async def list_articles() -> list[dict]:
    return []
```

This route serves `/api/articles/`.

### Route decorators

<!-- fragment -->
```python
api.get(path, *, name=None, response_model=..., status_code=None, validate_response=None,
        guards=None, auth=None, tags=None, summary=None, description=None,
        response_class=None, include_in_schema=None, deprecated=False)
```

`api.get()`, `api.post()`, `api.put()`, `api.patch()`, `api.delete()`, `api.head()`, `api.options()` and `api.query()` register a handler for one HTTP method.
They take the same arguments. Each decorator returns the handler with no change, so you can put more than one decorator on a handler.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path. `{name}` is a path parameter. `{name:path}` matches the remainder of the path, slashes included. The API `prefix` comes before it. |
| `name` | `str \| None` | `None` | The name for `reverse()`. `None` uses the name of the handler function. |
| `response_model` | `type \| dict \| None` | (not set) | The response type. When you do not set it, the return annotation is the response type. `None` sets no response type. A `dict` from status code to type documents more than one status. See [Declare the response type](../topics/responses.md#declare-the-response-type). |
| `status_code` | `int \| None` | `None` | The default status. `None` gives 200, or the lowest 2xx key of a `response_model` dict. |
| `validate_response` | `bool \| None` | `None` | Convert and validate the return value with the response type. `None` uses the `validate_response` of the `BoltAPI`. |
| `guards` | `list \| None` | `None` | The permission checks of the route. `None` uses the `BOLT_DEFAULT_PERMISSION_CLASSES` setting, or no check. See [Permissions](../topics/permissions.md). |
| `auth` | `list \| None` | `None` | The authentication backends of the route. `None` uses the `BOLT_AUTHENTICATION_CLASSES` setting. Without the setting, a route with a guard that needs a user gets `JWTAuthentication()`. See [Authentication](../topics/authentication.md). |
| `tags` | `list[str] \| None` | `None` | The OpenAPI tags. |
| `summary` | `str \| None` | `None` | The OpenAPI summary. `None` uses the first line of the handler docstring. |
| `description` | `str \| None` | `None` | The OpenAPI description. `None` uses the docstring after its first line. |
| `response_class` | `type \| None` | `None` | A class for the response of a generator handler. Use `EventSourceResponse` for [Server-Sent Events](../topics/sse.md). |
| `include_in_schema` | `bool \| None` | `None` | Show the route in the OpenAPI document. `None` uses the value of the view class, then the value of the `BoltAPI`. |
| `deprecated` | `bool` | `False` | Mark the operation as deprecated in the OpenAPI document. |

**Errors**

- `ValueError`: `status_code` is not from 200 to 599.
- `ValueError`: the path has one parameter name two times.
- `TypeError`: an argument has a type that Bolt cannot build from its source, or a `GET`, `HEAD`, `DELETE` or `OPTIONS` handler has a body argument.
- `TypeError`: a `response_model` dict has a key that is not an `int` or `...`.
- `ValueError` at startup: two routes have the same method and path, or two paths differ only in a parameter name.

**Example**

```python
import msgspec


class ArticleOut(msgspec.Struct):
    id: int
    title: str


@api.post("/articles", status_code=201, tags=["articles"], summary="Create an article")
async def create_article() -> ArticleOut:
    return ArticleOut(id=1, title="Hello")
```

### api.websocket()

<!-- fragment -->
```python
api.websocket(path, *, name=None, guards=None, auth=None, tags=None,
              summary=None, description=None, include_in_schema=None)
```

Register a WebSocket handler. The handler must be an `async` function.
A WebSocket route accepts its path with or without a trailing slash.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path. |
| `name` | `str \| None` | `None` | The name for `reverse()`. `None` uses the name of the handler function. |
| `guards` | `list \| None` | `None` | The permission checks of the handshake. |
| `auth` | `list \| None` | `None` | The authentication backends of the handshake. `SessionAuthentication` raises `ImproperlyConfigured`, because Django middleware does not run on a WebSocket. |
| `tags` | `list[str] \| None` | `None` | The OpenAPI tags. `None` gives the tag `WebSocket`. |
| `summary` | `str \| None` | `None` | The OpenAPI summary. |
| `description` | `str \| None` | `None` | The OpenAPI description. |
| `include_in_schema` | `bool \| None` | `None` | Show the route in the OpenAPI document. |

**Errors**

- `TypeError`: the handler is not an `async` function.

See [WebSocket](../topics/websocket.md).

### api.view()

<!-- fragment -->
```python
api.view(path, *, name=None, methods=None, guards=None, auth=None, status_code=None,
         validate_response=None, tags=None, include_in_schema=None, deprecated=False)
```

A class decorator that registers an `APIView` subclass. Each method of the class that has the name of an HTTP method, such as `get` or `post`, is one route.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path of each route of the view. |
| `name` | `str \| None` | `None` | The name for `reverse()`. `None` uses the class name. |
| `methods` | `list[str] \| None` | `None` | The methods to register. `None` registers each method that the class has. |
| `guards` | `list \| None` | `None` | The guards. `None` uses the `guards` attribute of the class. |
| `auth` | `list \| None` | `None` | The authentication backends. `None` uses the `auth` attribute of the class. |
| `status_code` | `int \| None` | `None` | The default status. `None` uses the `status_code` attribute of the class. |
| `validate_response` | `bool \| None` | `None` | `None` uses the class attribute, then the `BoltAPI`. |
| `tags` | `list[str] \| None` | `None` | The OpenAPI tags. |
| `include_in_schema` | `bool \| None` | `None` | `None` uses the class attribute, then the `BoltAPI`. |
| `deprecated` | `bool` | `False` | Mark each operation as deprecated. |

**Errors**

- `TypeError`: the class is not an `APIView` subclass.
- `ValueError`: the class has no method handler, `methods` is empty, or `methods` names a method that the class does not have.

See [Class-based views](../topics/class-based-views.md).

### api.viewset()

<!-- fragment -->
```python
api.viewset(path, *, name=None, guards=None, auth=None, status_code=None,
            validate_response=None, lookup_field="pk", tags=None,
            include_in_schema=None, deprecated=False)
```

A class decorator that registers a `ViewSet` subclass. Each action that the class has is one route:

| Action | Route | Default status |
| --- | --- | --- |
| `list` | `GET {path}` | 200 |
| `create` | `POST {path}` | 201 |
| `retrieve` | `GET {path}/{lookup_field}` | 200 |
| `update` | `PUT {path}/{lookup_field}` | 200 |
| `partial_update` | `PATCH {path}/{lookup_field}` | 200 |
| `destroy` | `DELETE {path}/{lookup_field}` | 204 |

A method with the `@action` decorator is also a route.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path of the collection. |
| `name` | `str \| None` | `None` | The first part of each route name. The route names are `{name}-{action}`, for example `article-list`. `None` uses the class name. |
| `guards` | `list \| None` | `None` | The guards of each route. An `@action` that sets `guards` keeps its value. |
| `auth` | `list \| None` | `None` | The authentication backends of each route. An `@action` that sets `auth` keeps its value. |
| `status_code` | `int \| None` | `None` | The status of each route, in place of the defaults above. An `@action` that sets `status_code` keeps its value. |
| `validate_response` | `bool \| None` | `None` | The response validation of each route. An `@action` that sets it keeps its value. |
| `lookup_field` | `str` | `"pk"` | The name of the path parameter of a detail route. A value other than `"pk"` wins over the `lookup_field` attribute of the class. |
| `tags` | `list[str] \| None` | `None` | The OpenAPI tags of each route. An `@action` that sets `tags` keeps its value. |
| `include_in_schema` | `bool \| None` | `None` | `None` uses the class attribute, then the `BoltAPI`. |
| `deprecated` | `bool` | `False` | Mark each operation as deprecated, the `@action` routes included. |

**Errors**

- `TypeError`: the class is not a `ViewSet` subclass.

See [Class-based views](../topics/class-based-views.md).

### api.include_router()

<!-- fragment -->
```python
api.include_router(router: Router, prefix: str = "") -> None
```

Register each route of `router` and of its child routers on the API.
The route path is the API `prefix`, then `prefix`, then the prefixes of the routers, then the route path.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `router` | `Router` | (required) | The router. |
| `prefix` | `str` | `""` | A path that comes before the prefix of the router. |

A router with a parent registers one time for each `prefix`, with the prefixes of its parents. To include the router or its parent gives the same routes.

**Errors**

- `TypeError`: `router` is not a `Router`. Use `mount()` for a `BoltAPI`.

### api.urls

<!-- fragment -->
```python
api.urls -> tuple[object, str | None, str | None]
```

The URLconf module, the app namespace and the instance namespace of the API, as `admin.site.urls` gives them.
Use it in `path()`, for example `path("blog/", api.urls)`. Bolt then serves each route of the API under `blog/`, and `reverse()` gives the same path.
The two namespaces are the `namespace` of the API. See [Mount an API in the URLconf](../topics/routing.md#mount-an-api-in-the-urlconf).

**Errors**

- `ImproperlyConfigured`: at startup, `runbolt` and the test clients refuse a prefix that Bolt cannot serve at static paths.
  Examples are `re_path()`, `i18n_patterns()`, a converter other than `str`, two parameters in one segment, one parameter two times, no trailing slash, and include keyword arguments.
  They also refuse an ASGI mount, a Django mount or an MCP server of the API under a prefix with a parameter.
  The message names the URLconf entry. Django alone does not refuse the prefix: `manage.py check` and `reverse()` work. The exception is one parameter name two times in a path: there `reverse()` raises the error of Django itself.
- `ValueError`: at startup, a handler or a dependency takes a prefix parameter as an argument with no marker. Read it from `request.params` or with `Path()`.

### api.mount()

<!-- fragment -->
```python
api.mount(path: str, app: BoltAPI) -> None
```

Copy each route of `app` into this API, under `path`. The routes keep the middleware, authentication and configuration of `app`.
The API `prefix` comes before `path`. The `trailing_slash` mode of this API applies to the copied routes.
The lifespan of `app` runs with this API. It starts after the lifespan of this API and stops before it.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path of the mounted routes. It can have a parameter, for example `/tenants/{tenant}`. |
| `app` | `BoltAPI` | (required) | The API to mount. |

**Errors**

- `TypeError`: `app` is not a `BoltAPI`.
- `ValueError`: `app` is this API, or `app` already has the routes of this API.
- `ValueError`: a handler or a dependency of `app` takes a parameter of `path` as an argument with no marker. Read it from `request.params` or with `Path()`.
- `ValueError`: `app` has a different `error_formatter`.

### api.mount_asgi()

<!-- fragment -->
```python
api.mount_asgi(path: str, app: Callable) -> None
```

Serve an ASGI application under a fixed path. Bolt sends a request to it when no Bolt route matches.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path. It cannot have a parameter. |
| `app` | `Callable` | (required) | The ASGI application. |

**Errors**

- `TypeError`: `app` is not callable.
- `ValueError`: `path` has a parameter, or another ASGI application has the same path.

See [ASGI mounts](../topics/asgi-mounts.md).

### api.mount_django()

<!-- fragment -->
```python
api.mount_django(path: str, app: Any | None = None, *, clear_root_path: bool = False) -> None
```

Serve Django's ASGI application under a path. It calls `mount_asgi()`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path, for example `"/admin"`. |
| `app` | `Any \| None` | `None` | The ASGI application. `None` uses `get_asgi_application()`. |
| `clear_root_path` | `bool` | `False` | Give Django an empty `root_path`. Use it when your URL patterns already have the mount path, for example `admin/`. |

See [ASGI mounts](../topics/asgi-mounts.md).

### api.mount_mcp()

<!-- fragment -->
```python
api.mount_mcp(mcp, path="/mcp", *, auth=None, guards=None, oauth=None, expose=None,
              allowed_hosts=None, allowed_origins=None) -> None
```

Serve an MCP server over Streamable HTTP at `path`. It needs the `bolt-mcp` package, and raises `ImportError` without it.
See [MCP](../topics/mcp.md) for each argument.

### api.native_route()

<!-- fragment -->
```python
api.native_route(method: str, path: str, capsule: Any) -> None
```

Serve `method` and `path` with a Rust handler from another extension module. The request does not go into Python.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `method` | `str` | (required) | The HTTP method. |
| `path` | `str` | (required) | The path. A parameter can have no type, or the type `path`. |
| `capsule` | `Any` | (required) | The `django_bolt.native_handler.v1` capsule of the handler. |

**Errors**

- `ValueError`: the method is not one of the eight methods of the route decorators, or a path parameter has a type other than `path`.

See [Native routes](../topics/native-routes.md).

## Router

<!-- fragment -->
```python
Router(
    prefix: str = "",
    tags: list[str] | None = None,
    middleware: list | None = None,
    auth: list | None = None,
    guards: list | None = None,
    parent: Router | None = None,
)
```

Make a group of routes. Register it on an API with `api.include_router()`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `prefix` | `str` | `""` | A path prefix for each route of the router. |
| `tags` | `list[str] \| None` | `None` | The OpenAPI tags of each route that does not set `tags`. |
| `middleware` | `list \| None` | `None` | Middleware for each route of the router. It runs after the middleware of the API and of the parent routers. |
| `auth` | `list \| None` | `None` | The authentication backends of each route that does not set `auth`. |
| `guards` | `list \| None` | `None` | The guards of each route that does not set `guards`. |
| `parent` | `Router \| None` | `None` | The parent router. The routes get the prefix and the middleware of the parent. |

A router takes the `tags`, `auth` and `guards` that it does not set from the nearest parent that sets them.

**Example**

```python
from django_bolt import BoltAPI, IsAuthenticated, Router

drafts = Router(prefix="/drafts", tags=["drafts"], guards=[IsAuthenticated()])


@drafts.get("")
async def list_drafts() -> list[dict]:
    return []


api = BoltAPI()
api.include_router(drafts)
```

### Route decorators

<!-- fragment -->
```python
router.get(path, **kwargs)
```

`router.get()`, `router.post()`, `router.put()`, `router.patch()`, `router.delete()`, `router.head()`, `router.options()` and `router.query()` register a handler on the router.
They take the keyword arguments of the [`BoltAPI` route decorators](#route-decorators).
Bolt checks the arguments and the handler when `api.include_router()` registers the routes.

### router.include_router()

<!-- fragment -->
```python
router.include_router(router: Router, prefix: str = "") -> None
```

Make `router` a child of this router. `prefix` comes before the prefix of the child.

### Other methods

- `router.get_all_routes()` returns a list of `(method, path, handler, options)` tuples for the router and its children.
- `router.child_routers()` returns each router below this router.
- `router.parent_prefix()` returns the prefixes of the parent routers, joined.
- `router.get_middleware_chain()` returns the middleware of the parents and of this router, in the order that they run.

## See also

- [Routing](../topics/routing.md)
- [Requests](../topics/requests.md)
- [Responses](../topics/responses.md)
- [Class-based views reference](views.md)
- [Settings reference](settings.md)

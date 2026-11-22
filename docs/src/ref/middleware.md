---
description: Reference for django_bolt.middleware. Middleware base classes, the route decorators, CORS, rate limits, compression and the Django middleware adapters.
---

# Middleware reference

This page lists the public API of `django_bolt.middleware`. For a task-based introduction, see [Middleware](../topics/middleware.md), [CORS](../topics/cors.md), [Rate limiting](../topics/rate-limiting.md) and [Compression](../topics/compression.md).

Each name on this page is in `django_bolt.middleware`. `BaseMiddleware`, `Middleware`, `MiddlewareProtocol`, `TimingMiddleware`, `LoggingMiddleware`, `ErrorHandlerMiddleware`, `DjangoMiddleware`, `CompressionConfig` and the five decorators are also in `django_bolt`.

## Where middleware goes

| Place | Argument | Accepts |
| --- | --- | --- |
| `BoltAPI` | `middleware: list \| None = None` | Middleware classes, `DjangoMiddleware`, `DjangoMiddlewareStack`, `cors` and `rate_limit` dicts |
| `BoltAPI` | `django_middleware: bool \| list[str] \| dict \| None = None` | See [load_django_middleware](#load_django_middleware) |
| `BoltAPI` | `compression: CompressionConfig \| None \| False = None` | See [CompressionConfig](#compressionconfig) |
| `Router` | `middleware: list \| None = None` | The same as `BoltAPI(middleware=...)` |
| A route | `@middleware(...)` | The same, plus function middleware |

**Errors.** `TypeError` for an instance of a middleware class, a class with no `__call__`, a function middleware outside a route, or an entry of another kind.
`ValueError` for a dict with no `type`, or with a `type` that is not `"cors"` or `"rate_limit"`.

## Python middleware

### Middleware

<!-- fragment -->
```python
class Middleware(ABC):
    def __init__(self, get_response: GetResponse) -> None: ...
    async def process_request(self, request: Request) -> Response: ...  # abstract
```

The base class of a Bolt middleware. Override `process_request`. Call `await self.get_response(request)` to run the rest of the chain and the handler.
Return its result, a response object such as `JSON`, or another value that a handler can return.

`get_response()` returns an object with `status_code: int` and `headers`, a dict whose names ignore letter case.
Bolt makes the instance with the next item of the chain as `get_response`, and reuses it for many requests.

### BaseMiddleware

<!-- fragment -->
```python
class BaseMiddleware(ABC):
    exclude_paths: list[str] | None = None
    exclude_methods: list[str] | None = None

    def __init__(self, get_response: GetResponse) -> None: ...
    async def process_request(self, request: Request) -> Response: ...  # abstract
```

`Middleware` with two class attributes. A request that matches one of them goes to `get_response()` and skips `process_request`.

| Attribute | Description |
| --- | --- |
| `exclude_paths` | Path patterns. `*` matches one segment, with no `/`. `**` matches any text. The pattern matches the whole path. |
| `exclude_methods` | Method names, in any case |

### MiddlewareProtocol

A class with `__init__(self, get_response)` and `async __call__(self, request)`. A class of this form needs no base class. `Middleware` and `BaseMiddleware` implement it.

`GetResponse` is the type of `get_response`: `Callable[[Request], Awaitable[Response]]`. `MiddlewareType` is the type of an entry of a `middleware` list.

### TimingMiddleware

A `BaseMiddleware`. It sets `request.state["request_id"]` to a new UUID4 string and `request.state["start_time"]` to `time.perf_counter()`.
It adds the response headers `X-Request-ID` and `X-Response-Time`, for example `0.0012s`.

### LoggingMiddleware

<!-- fragment -->
```python
LoggingMiddleware(
    get_response,
    logger: logging.Logger | None = None,
    log_body: bool = False,
    log_headers: bool = False,
    log_level: int = logging.INFO,
)
```

A `BaseMiddleware`. It logs a `Request: {...}` line and a `Response: ...` line for each request. The default logger is `django_bolt.requests`.
`exclude_paths` is `["/health", "/metrics", "/docs", "/openapi.json"]`. With `log_headers=True`, it logs all request headers, also `Authorization`.
To change an argument, subclass it and pass the value to `super().__init__()`.
This class is not the Rust access log of Bolt. See [Logging](../topics/logging.md).

### ErrorHandlerMiddleware

<!-- fragment -->
```python
ErrorHandlerMiddleware(get_response, debug: bool = False)
```

A `BaseMiddleware`. It logs an unhandled exception to the `django_bolt.errors` logger and raises `HTTPException(500)`.
The `detail` is `"Internal Server Error"`, or the traceback when `debug` is `True`. An `HTTPException` or a validation error passes through.

## Decorators

Put each decorator below the route decorator. A decorator above it raises `TypeError` at startup.

### middleware

<!-- fragment -->
```python
@middleware(*middleware_classes)
@middleware(type="cors" | "rate_limit", **options)
@middleware  # on a function (request, call_next)
```

Adds middleware to one route.

- With classes or wrappers, it adds them in order. The first argument runs first.
- With keyword arguments, it adds one dict config. `type` is `"cors"` or `"rate_limit"`, and the options are the keys of [the dict configs](#dict-configs).
- On an async function `(request, call_next)`, it returns a route decorator. `call_next(request)` runs the rest of the chain.

With more than one `@middleware` on a route, the decorator nearest to the function runs first.

**Errors.** `TypeError` for a keyword argument with no `type`, an entry that is not a middleware, or an option of the wrong type.
`ValueError` for an unknown `type`, an option that `@cors` or `@rate_limit` refuses, or a second `cors` or `rate_limit` config on the function.

### skip_middleware

<!-- fragment -->
```python
@skip_middleware(*middleware_names: str | type)
```

Removes middleware from one route. A name ignores letter case.

| Name | Removes |
| --- | --- |
| `"cors"`, `"rate_limit"`, `"compression"` | The built-in Rust middleware of that name |
| A class, its `__name__` or its dotted path | That Python middleware or Django middleware class |
| The `__name__` or the dotted path of a function | That function middleware |
| `"*"` | Each Bolt middleware, Python and Rust. Not Django middleware. |

`@skip_middleware` does not remove authentication or guards.

**Errors.** `TypeError` for a value that is not a `str` or a class.

### no_compress

<!-- fragment -->
```python
@no_compress
```

The same as `@skip_middleware("compression")`. The route sends no `Content-Encoding`, for buffered and streaming responses.

### cors

<!-- fragment -->
```python
@cors(
    origins: list[str] | str | None = None,
    methods: list[str] | None = None,
    headers: list[str] | None = None,
    credentials: bool = False,
    max_age: int = 3600,
    origin_regexes: list[str] | str | None = None,
)
```

Gives one route its own CORS config. Rust adds the headers.

| Parameter | Default | Description |
| --- | --- | --- |
| `origins` | `None` | The allowed origins. `["*"]` allows each origin. A string is one origin. |
| `origin_regexes` | `None` | Patterns of allowed origins, in the syntax of the Rust `regex` crate. Start with `^` and end with `$`. |
| `methods` | `None` | The methods of a preflight answer. `None` means `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `QUERY`. |
| `headers` | `None` | The request headers of a preflight answer. `None` means `Content-Type`, `Authorization`. |
| `credentials` | `False` | Send `access-control-allow-credentials: true` |
| `max_age` | `3600` | The `access-control-max-age` of a preflight answer, 0 to 4294967295 |

The origins and patterns of the route replace `CORS_ALLOWED_ORIGINS` and `CORS_ALLOWED_ORIGIN_REGEXES`. An option that you do not give gets its default, not the value of a setting.
With `"*"` and `credentials=True`, Bolt copies the `Origin` of each request into the answer, and warns with `RuntimeWarning`.

**Errors.** `ValueError` for no origin and no pattern, an empty pattern, a pattern that does not compile, a `max_age` out of range, a name that is not valid in a header, or a second `cors` config on the function.
`TypeError` for an origin or a pattern that is not a `str`, `methods` or `headers` that is not a list of `str`, a `credentials` that is not a `bool`, or a `max_age` that is not an `int`.

### rate_limit

<!-- fragment -->
```python
@rate_limit(rps: int = 100, burst: int | None = None, key: str = "ip")
```

Limits one route with a token bucket in Rust.

| Parameter | Default | Description |
| --- | --- | --- |
| `rps` | `100` | Tokens added each second. An `int` of 1 or more. |
| `burst` | `None` | The size of the bucket. `None` means `2 * rps`. An `int` of 1 or more. |
| `key` | `"ip"` | `"ip"`, `"user"`, `"api_key"`, or the name of a request header. The names have no case. |

A rejected request gets 429 with the headers `Retry-After`, `X-RateLimit-Limit` (`rps`) and `X-RateLimit-Burst` (`burst`), and this body:

```json
{"detail": "Rate limit exceeded. Try again in 1 seconds.", "retry_after": 1}
```

`"ip"` uses the client address from [`BOLT_TRUSTED_PROXIES`](settings.md). An IPv6 client counts per network of `BOLT_RATE_LIMIT_IPV6_PREFIX` bits (64).
`"user"` and `"api_key"` run after authentication. A request with no identity counts per client address. A header key counts all requests with no such header in one bucket.

**Errors.** `TypeError` for an `rps` or `burst` that is not an `int`, or a `key` that is not a `str`. `ValueError` for a count less than 1, an empty `key`, a `key` that is not a header name, or a second `rate_limit` config on the function.
Startup raises `ValueError` for `"user"` or `"api_key"` on a route with no auth backend.

## Dict configs

The API, a router and `@middleware(...)` take a `cors` or a `rate_limit` config as a dict. Each key is a keyword argument of the decorator, plus `type`.

| `type` | Keys |
| --- | --- |
| `"cors"` | `origins`, `origin_regexes`, `methods`, `headers`, `expose_headers`, `credentials`, `max_age` |
| `"rate_limit"` | `rps`, `burst`, `key` |

`expose_headers` is a list of response headers that the page can read. Only the dict form has it.
A dict of a route needs `origins` or `origin_regexes`. A dict of an API or a router with neither keeps the allow list of the outer config.
The most specific config of each type wins: the route, then the router, then the API. A `None` value is a missing value.

**Errors.** The checks of the decorator, and `TypeError` for an unknown key.

## CompressionConfig

<!-- fragment -->
```python
@dataclass
class CompressionConfig:
    backend: Literal["gzip", "brotli", "zstd"] = "brotli"
    minimum_size: int = 500
    gzip_fallback: bool = True
    brotli_level: int = 5
    brotli_lgwin: int = 14
    gzip_level: int = 6
    zstd_level: int = 3
```

The compression config of the server.

| Field | Default | Description |
| --- | --- | --- |
| `backend` | `"brotli"` | The encoding that Bolt uses when the client accepts it |
| `minimum_size` | `500` | The smallest buffered body, in bytes, that Bolt compresses. No effect on a stream. |
| `gzip_fallback` | `True` | Use gzip when the client does not accept `backend` |
| `brotli_level` | `5` | 0 to 11. Streaming responses only. |
| `brotli_lgwin` | `14` | 10 to 24. The window is `2 ** brotli_lgwin` bytes for each stream. Streaming responses only. |
| `gzip_level` | `6` | 0 to 9. Streaming responses only. |
| `zstd_level` | `3` | 1 to 22. Streaming responses only. |

A buffered response uses fixed levels: brotli 3 with a 4 MiB window, gzip 1 and zstd 3.

The server uses one config. `settings.BOLT_COMPRESSION` wins when it is set. `None` or `False` there turns compression off.
Otherwise the server uses the `compression` of the first API with compression on. `BoltAPI(compression=False)` turns it off for that API.

**Errors.** `ValueError` for an unknown `backend`, a negative `minimum_size`, or a level out of range. `BoltAPI(compression=...)` raises `TypeError` for a value that is not a `CompressionConfig`, `None` or `False`. `BOLT_COMPRESSION` raises `ImproperlyConfigured` for such a value.

## Django middleware

### DjangoMiddleware

<!-- fragment -->
```python
DjangoMiddleware(middleware_class: type | str, **init_kwargs)
```

Runs one Django middleware class in a Bolt `middleware` list. A string is the dotted path of the class. Bolt passes `init_kwargs` to the constructor of the class.

Bolt runs `process_request`, `process_view`, `process_response` and `process_exception`, and the `__call__` of a new-style middleware. It does not run `process_template_response`.
`process_view` gets the handler as the view. On a request thread (a lane), Bolt makes a second instance of the class in sync mode.

**Errors.** `TypeError` for a callable that is not a class. `ImportError` for a dotted path that does not import. `RuntimeError` for a class with `sync_capable` and `async_capable` both false.

### DjangoMiddlewareStack

<!-- fragment -->
```python
DjangoMiddlewareStack(middleware_classes: list[type | str])
```

Runs a list of Django middleware classes as one Bolt middleware, in the hook order of Django: each `process_request`, then each `process_view`, then each `process_response` in reverse order.
It converts the request and the response one time for the whole list. An entry can be a dotted path.

**Errors.** The same as `DjangoMiddleware`.

### load_django_middleware

<!-- fragment -->
```python
load_django_middleware(config: bool | list[str] | dict = True, *, exclude_defaults: bool = True) -> list
```

`BoltAPI(django_middleware=config)` calls it. It returns a list with one `DjangoMiddlewareStack`, or an empty list.

| `config` | Classes |
| --- | --- |
| `True` | Each entry of `settings.MIDDLEWARE`, in order |
| `False` or `None` | None |
| A list of dotted paths | These classes, in this order. They need not be in `settings.MIDDLEWARE`. |
| `{"include": [...]}` | The entries of `settings.MIDDLEWARE` that are in the list, in the order of the setting |
| `{"exclude": [...]}` | The entries of `settings.MIDDLEWARE` that are not in the list |

With `exclude_defaults=True`, it also leaves out `DEFAULT_EXCLUDED_MIDDLEWARE`, an empty set.

**Errors.** `ImproperlyConfigured` for an entry that is not a dotted path string. `ImportError` for a path that does not import.

### get_django_middleware_setting

<!-- fragment -->
```python
get_django_middleware_setting() -> list[str]
```

Returns a copy of `settings.MIDDLEWARE`, or `[]`.

## See also

- [Middleware](../topics/middleware.md)
- [CORS](../topics/cors.md)
- [Rate limiting](../topics/rate-limiting.md)
- [Compression](../topics/compression.md)
- [Settings reference](settings.md)

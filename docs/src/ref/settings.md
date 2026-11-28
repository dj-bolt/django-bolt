---
description: Every Django setting and DJANGO_BOLT_* environment variable that Django-Bolt reads, with its type, its default and its effect.
---

# Settings

Django-Bolt reads its configuration from your Django settings and from `DJANGO_BOLT_*` environment variables.
This page lists each one. The guides explain how to use them.

## How Bolt reads settings

Bolt reads most settings one time, when the server starts. `runbolt` and `TestClient` read them with the same code.
A change after startup has no effect, unless the entry says otherwise.

A missing setting gives its default. A setting with the wrong type stops startup with `ImproperlyConfigured`. The error names the setting and the value:

```text
django.core.exceptions.ImproperlyConfigured: BOLT_STATIC_MAX_AGE must be an int of 0 or more, got str '3600'.
```

- A size, a count or a time in seconds is an `int`. A `str` such as `"3600"` is not an `int`. A `bool` is not an `int`.
- A flag such as `CORS_ALLOW_CREDENTIALS` is a `bool`.
- `DEBUG` is a `bool` or an `int`. A `str` such as `"False"` stops startup, because Django reads a non-empty `str` as true.
- A list setting is a list of `str`. One `str` is not a list.

Convert a value that you read from the environment:

```python title="blog/settings.py"
import os

BOLT_MAX_UPLOAD_SIZE = int(os.environ.get("BOLT_MAX_UPLOAD_SIZE", 10 * 1024 * 1024))
```

## API discovery

### BOLT_API

Default: not set. Type: `list[str]`.

The `BoltAPI` instances that `runbolt` serves, as `"module:attribute"` paths:

```python
BOLT_API = ["blog.api:api", "comments.api:api"]
```

When the setting is there, `runbolt` does not look for other APIs.
An entry that does not import, or that is not a `BoltAPI`, stops startup with `CommandError`.
Without the setting, `runbolt` looks in the `api.py` and `bolt_api.py` modules of the project and of each installed app.
See [Routing](../topics/routing.md).

## Request limits

### BOLT_MAX_UPLOAD_SIZE

Default: `1048576` (1 MiB). Type: `int`, 0 or more.

The largest request body in bytes. A larger body gets `413` with `{"detail":"Payload too large"}`, before the handler runs.
It is also the limit of each uploaded file. `File(max_size=...)` can set a smaller limit for one route, not a larger one.
See [Requests](../topics/requests.md).

```python
from django_bolt import FileSize

BOLT_MAX_UPLOAD_SIZE = FileSize.MB_10
```

### BOLT_MEMORY_SPOOL_THRESHOLD

Default: `1048576` (1 MiB). Type: `int`, 0 or more.

An uploaded file up to this size stays in memory. A larger file goes to a temporary file on disk.
Bolt reads it when it registers each route.

### BOLT_MAX_HEADER_SIZE

Default: `8192`. Type: `int`, 0 or more.

The longest header value in bytes that a route reads. A longer value gets `400` with `{"detail":"Header value too large (max 8192 bytes)"}`.
A request with more than 100 headers also gets `400`.

### DJANGO_BOLT_MAX_PARAM_LENGTH

This limit is an environment variable. See [Environment variables](#environment-variables).

## Authentication and permissions

### BOLT_AUTHENTICATION_CLASSES

Default: not set. Type: `list` of authentication backend instances.

The authentication backends of each route that has no `auth` argument.
Without the setting, a route whose guard needs a user, such as `IsAuthenticated()`, gets `JWTAuthentication()`. Each other route gets no authentication.
Set `[]` to give no route a default backend.
Bolt reads the setting when it registers each route.
See [Authentication](../topics/authentication.md).

```python
from django_bolt.auth import JWTAuthentication

BOLT_AUTHENTICATION_CLASSES = [JWTAuthentication()]
```

### BOLT_DEFAULT_PERMISSION_CLASSES

Default: `[AllowAny()]`. Type: `list` of guard instances.

The guards of each route that has no `guards` argument.
Bolt reads the setting when it registers each route.
See [Permissions](../topics/permissions.md).

```python
from django_bolt.auth import IsAuthenticated

BOLT_DEFAULT_PERMISSION_CLASSES = [IsAuthenticated()]
```

### SECRET_KEY

Bolt uses the Django `SECRET_KEY` to sign and verify JWTs, unless the backend gets its own `secret`.

## Proxies and rate limits

### BOLT_TRUSTED_PROXIES

Default: `[]`. Type: `list` or `tuple` of `str`.

The proxies in front of Bolt, as IP addresses or CIDR blocks, such as `"127.0.0.1"` or `"10.0.0.0/8"`.

```python
BOLT_TRUSTED_PROXIES = ["127.0.0.1"]
```

Bolt reads `X-Forwarded-For`, `X-Real-IP` and `X-Forwarded-Proto` only from a peer in this list:

- With an empty list, the client address is the peer address of the connection. Bolt ignores each forwarding header.
- From a listed peer, Bolt reads `X-Forwarded-For` from the right. The first address that is not a listed proxy is the client.
- From a listed peer, the rightmost `X-Forwarded-Proto` value sets the scheme, when it is `http` or `https`.

The client address goes to `request.META["REMOTE_ADDR"]` and to `@rate_limit(key="ip")`.
The scheme goes to `request.scheme`, `request.is_secure()` and the ASGI scope of a mounted Django app.
An entry that is not an address or a block stops startup with `ImproperlyConfigured`.
See [Deployment](../topics/deployment.md#run-behind-a-reverse-proxy).

### BOLT_RATE_LIMIT_IPV6_PREFIX

Default: `64`. Type: `int`, 0 to 128.

The prefix length of the network that a rate limit counts as one IPv6 client.
Set `128` to count each IPv6 address. An IPv4 client is always counted by its address.
See [Rate limiting](../topics/rate-limiting.md).

## CORS

Bolt reads the settings of `django-cors-headers`, with the same names.
A route with `@cors` uses its own values. See [CORS](../topics/cors.md).

| Setting | Type | Default |
| --- | --- | --- |
| `CORS_ALLOWED_ORIGINS` | `list[str]` | `[]` |
| `CORS_ALLOWED_ORIGIN_REGEXES` | `list[str]` | `[]` |
| `CORS_ALLOW_ALL_ORIGINS` | `bool` | `False` |
| `CORS_ALLOW_CREDENTIALS` | `bool` | `False` |
| `CORS_ALLOW_METHODS` | `list[str]` | `["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "QUERY"]` |
| `CORS_ALLOW_HEADERS` | `list[str]` | `["Content-Type", "Authorization"]` |
| `CORS_EXPOSE_HEADERS` | `list[str]` | `[]` |
| `CORS_PREFLIGHT_MAX_AGE` | `int`, 0 to 4294967295 | `3600` |

Without `CORS_ALLOWED_ORIGINS`, `CORS_ALLOWED_ORIGIN_REGEXES` and `CORS_ALLOW_ALL_ORIGINS`, Bolt adds no CORS headers.
A pattern in `CORS_ALLOWED_ORIGIN_REGEXES` that does not compile stops startup.
With `CORS_ALLOW_ALL_ORIGINS` and `CORS_ALLOW_CREDENTIALS` both `True`, Bolt prints a warning and sends the origin of the request in place of `*`.

## Compression

### BOLT_COMPRESSION

Default: not set. Type: `CompressionConfig`, `None` or `False`.

The compression of each response of the server. The setting wins over the `compression` argument of `BoltAPI`.
`None` or `False` turns compression off. Another value stops startup with `ImproperlyConfigured`.
Without the setting, the server uses the configuration of the first API. A `BoltAPI` compresses with brotli by default.
See [Compression](../topics/compression.md).

```python
from django_bolt import CompressionConfig

BOLT_COMPRESSION = CompressionConfig(backend="gzip", minimum_size=1000)
```

## Static and media files

### STATIC_URL, STATIC_ROOT and STATICFILES_DIRS

The Django settings. With `STATIC_URL`, Bolt serves the files of `STATIC_ROOT` and then of `STATICFILES_DIRS` under that prefix.
Bolt removes `FORCE_SCRIPT_NAME` from the front of `STATIC_URL`, because the proxy removes it from the request path.
With `DEBUG = True`, Bolt also asks the Django staticfiles finders.
See [Static files](../topics/static-files.md).

### MEDIA_URL and MEDIA_ROOT

The Django settings. When `MEDIA_URL` starts with `/` and `MEDIA_ROOT` is an absolute path to a directory, Bolt serves the files of `MEDIA_ROOT` under that prefix.
Bolt removes `FORCE_SCRIPT_NAME` from the front of `MEDIA_URL`, as from `STATIC_URL`.
See [Media files](../topics/media-files.md).

### BOLT_STATIC_MAX_AGE

Default: not set. Type: `int`, 0 or more, or `None`.

Add `Cache-Control: public, max-age=N` to each `2xx` response of a static file. `None` adds no header.

### BOLT_MEDIA_MAX_AGE

Default: not set. Type: `int`, 0 or more, or `None`.

Add `Cache-Control: private, max-age=N` to each `2xx` response of a media file. `None` adds no header.

### SECURE_CSP

Default: not set. Type: `dict`.

The Django `SECURE_CSP` setting. Bolt sends this policy in a `Content-Security-Policy` header on each static and media response.
Bolt removes `CSP.NONCE`, because a file has no nonce.
A value that is not a `dict` stops startup with `ImproperlyConfigured`.

### BOLT_ALLOWED_FILE_PATHS

Default: not set. Type: `list` or `tuple` of absolute paths, as `str` or `Path`.

The directories that a `FileResponse` can serve from. A path outside them gets `403`.
`None` or `[]` sets no limit. A relative path or one `str` stops startup with `ImproperlyConfigured`.
`runbolt` reads the setting at startup. Under `TestClient`, the first `FileResponse` reads it, and `override_settings` changes it.
See [Responses](../topics/responses.md).

```python
BOLT_ALLOWED_FILE_PATHS = ["/srv/blog/exports"]
```

## Streaming and WebSocket

### BOLT_MAX_SYNC_STREAMING_THREADS

Default: `1000`. Type: `int`, 1 or more.

The number of sync generators that stream at the same time in one process. Each one uses one thread.
Above the limit, a new stream sends `retry: 30000` and closes.
`DJANGO_BOLT_MAX_SYNC_STREAMING_THREADS` wins over this setting.
See [Server-sent events](../topics/sse.md).

### WebSocket limits

An environment variable with the name of the setting and the prefix `DJANGO_` wins over the setting. For example, `DJANGO_BOLT_WS_MAX_CONNECTIONS` wins over `BOLT_WS_MAX_CONNECTIONS`.
See [WebSocket](../topics/websocket.md).

| Setting | Type | Default | Effect |
| --- | --- | --- | --- |
| `BOLT_WS_MAX_CONNECTIONS` | `int`, 0 or more | `10000` | Open connections in one process. A new upgrade above the limit gets `503`. |
| `BOLT_WS_CHANNEL_SIZE` | `int`, 1 or more | `100` | Messages that wait for the handler on one connection. |
| `BOLT_WS_HEARTBEAT_INTERVAL` | `int`, 1 or more | `5` | Seconds between two pings. |
| `BOLT_WS_CLIENT_TIMEOUT` | `int`, 0 or more | `10` | Seconds with no message and no pong from the client before Bolt closes the connection. |
| `BOLT_WS_MAX_MESSAGE_SIZE` | `int`, 0 or more | `1048576` | Largest message in bytes. A larger message closes the connection with code `1009`. |

## Mounts and native routes

### BOLT_ASGI_MOUNT_TIMEOUT

Default: `30`. Type: `int` or `float`, more than 0.

Seconds that a mounted ASGI app has to complete a request. After it, Bolt sends `504`.
See [ASGI mounts](../topics/asgi-mounts.md).

### BOLT_NATIVE_ROUTE_TIMEOUT

Default: `30`. Type: `int` or `float`, more than 0.

Seconds that a native route has to complete a request. After it, Bolt sends `504`.
See [Native routes](../topics/native-routes.md).

## Logging

### LOGGING

The Django setting. When your settings module defines `LOGGING`, Bolt does not change the logging configuration.
See [Logging](../topics/logging.md).

### BOLT_LOG_QUEUE_SIZE

Default: `10000`. Type: `int`, 1 or more.

Only without `LOGGING`. The size of the queue between the loggers and the console.
When the queue is full, Bolt drops new records and writes `django-bolt: log queue saturated, dropped N record(s)` to stderr.
A value that is not valid prints a warning to stderr, and Bolt uses `10000`.

### DJANGO_BOLT_LOG_SAMPLE

Default: not set. Type: `int` or `float`, 0.0 to 1.0.

This is a Django setting, not an environment variable.
The part of the 1xx to 3xx responses that write an access-log line. A 4xx or a 5xx response always writes its line.
It applies to each API with no `logging_config`. It is the `sample_rate` of `LoggingConfig`.

### DJANGO_BOLT_LOG_SLOW_MS

Default: not set. Type: `int` or `float`, 0 or more.

This is a Django setting, not an environment variable.
A 1xx to 3xx response writes an access-log line only when it took this number of milliseconds or more.
It applies to each API with no `logging_config`. It is the `min_duration_ms` of `LoggingConfig`.

## Async ORM

### BOLT_ASYNC_ORM_ON_LOOP

Default: `False`. Type: `bool`.

Run these reads on the event loop, with no thread: a QuerySet that a handler returns, `async for` over a QuerySet, `aiterator()`, `aget()`, `afirst()`, `alast()`, `aexists()`, `acount()`, `aaggregate()`, the count of the rows and the rows of a page for `@paginate`, and `await request.auser()`. The setting applies to PostgreSQL with psycopg 3.
It has no effect on Windows.
See [Async ORM](../topics/async-orm.md#run-async-reads-on-the-event-loop).

## Garbage collector

### BOLT_GC_FREEZE

Default: `True`. Type: `bool`.

`runbolt` calls `gc.freeze()` in each server process when the startup is complete, before the process takes requests.
The garbage collector of Python then does not read the objects of the startup: the modules, the models and the routes.
A full collection is thus much shorter, and the server answers no request during a collection.
Measured with one process and 2,000 requests in one second: a full collection took 30 to 45 ms with `False`, and 1 to 4 ms with `True`.

Reference counts still free a frozen object. The collector does not free a frozen object in a reference cycle.
Set `False` when your application drops large objects of its startup later, and these objects have reference cycles.
`TestClient` does not freeze objects.

## Signals

### BOLT_EMIT_SIGNALS

Default: `False`. Type: `bool`.

Send the Django `request_started` and `request_finished` signals for each request.
Bolt reads the setting when you create a `BoltAPI`.
See [Signals](../topics/signals.md).

## Development

### BOLT_DEV_FORCE_POLLING

Default: `False`. Type: `bool`.

Make `runbolt --dev` read the files every 500 ms, in place of file events.
Use it when file events do not arrive, for example in a Docker container on a Windows host.
See [runbolt](runbolt.md#development-mode).

## Django settings that change Bolt

| Setting | Effect on Bolt |
| --- | --- |
| `DEBUG` | Turns on the staticfiles finders for static files, error details in responses, and the `DEBUG` level of the default logging. |
| `ROOT_URLCONF` | `runbolt` looks for the project API next to it, and finds the admin prefix in it. |
| `MIDDLEWARE` | `BoltAPI(django_middleware=True)` loads it. See [Middleware](../topics/middleware.md). |
| `FORCE_SCRIPT_NAME` | The sub-path that a proxy removes. `request.path`, `WebSocket.path`, `reverse()`, redirects and the docs put it first. Bolt serves `STATIC_URL` and `MEDIA_URL` without it. Bolt reads it at startup, and refuses a value that Django percent-encodes. See [Deployment](../topics/deployment.md#serve-under-a-sub-path). |
| `DATABASES` | Sets the default size of the ORM thread pool. `OPTIONS["pool"]`, `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` change how Bolt keeps connections. See [Deployment](../topics/deployment.md#configure-database-connections). |

## Environment variables

Bolt reads its environment variables one time, at startup. An unset or empty variable gives the default.
A value that does not parse stops startup with `ImproperlyConfigured`. `DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT` is different: it raises `ValueError` when you create a `TestClient`.

```text
django.core.exceptions.ImproperlyConfigured: DJANGO_BOLT_ORM_THREADS must be an int of 1 or more, got '0'.
```

### Server

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `DJANGO_BOLT_WORKERS` | int, 1 or more | `1` | Worker threads in one process. `runbolt` sets it from `--workers`. |
| `DJANGO_BOLT_BACKLOG` | int, 0 or more | `1024` | Listen queue of the socket. `runbolt` sets it from `--backlog`. |
| `DJANGO_BOLT_KEEP_ALIVE` | int, 0 or more | no timeout | Keep-alive timeout in seconds. `--keep-alive` wins. |
| `DJANGO_BOLT_SHUTDOWN_TIMEOUT` | int, 0 or more | `30` | Seconds that the server drains after `SIGTERM`. Wins over `--workers-kill-timeout` for the drain. |
| `DJANGO_BOLT_REUSE_PORT` | `1`, `0`, `true` or `false` | `false` | Bind with `SO_REUSEPORT`. `runbolt` sets it in each worker of a supervisor. |
| `DJANGO_BOLT_PROCESS_ID` | int | not set | Set by `runbolt` in each worker of a supervisor: the slot of the worker. |
| `DJANGO_BOLT_BLOCKING_THREADS` | int, 1 or more | `1024` | Largest number of blocking threads of the Tokio runtime in one process. |

See [runbolt](runbolt.md) for the options.

### Thread pools and request lanes

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `DJANGO_BOLT_ORM_THREADS` | int, 1 or more | `1` when each database is SQLite, else `4` | Threads of the ORM pool of one process. It runs the QuerySets that async handlers return, `await request.auser()`, and the async ORM calls of a route with no Django middleware. |
| `DJANGO_BOLT_EXECUTOR_THREADS` | int, 1 or more | CPU count + 4, at most `32` | Threads of the shared pool of one process. Blocking sync handlers and `sync_to_thread` calls run on it. |
| `DJANGO_BOLT_LANE_IDLE_SECONDS` | number, more than 0 | `10` | Idle seconds after which a request lane closes its database connections and stops. |
| `DJANGO_BOLT_LOOP_CONNECTIONS` | int, 1 or more | `10` | Largest number of connections of one worker thread to one PostgreSQL database, for [`BOLT_ASYNC_ORM_ON_LOOP`](#bolt_async_orm_on_loop). |
| `DJANGO_BOLT_LOOP_PIPELINE_DEPTH` | int, 1 or more | `16` | Largest number of statements in flight on one loop connection. With `1`, a connection runs one statement at a time. See [Statements in flight on one connection](../topics/async-orm.md#statements-in-flight-on-one-connection). |
| `DJANGO_BOLT_LOOP_ACQUIRE_SECONDS` | number, more than 0 | `30` | Seconds that a read waits for a place on a loop connection. Then it fails with `OperationalError`. |
| `DJANGO_BOLT_LOOP_IDLE_SECONDS` | number, more than 0 | `600` | Idle seconds after which a loop connection closes. |
| `DJANGO_BOLT_LOOP_LIFETIME_SECONDS` | number, more than 0 | `1800` | Age in seconds after which a loop connection closes before its next use. |

See [Async ORM](../topics/async-orm.md) and [Middleware](../topics/middleware.md).

### Requests and streaming

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `DJANGO_BOLT_MAX_PARAM_LENGTH` | int, 1 to 1048576 | `8192` | Longest value in bytes of a path, query or form parameter. A longer value gets `422`. For a query or form value, the limit counts the bytes that the client sent: a query or URL-encoded value after the percent-decoding, a multipart value as it is. For a path value, the limit counts the bytes of the text. The text that the handler gets can have more bytes when the value is not UTF-8. Each bad byte becomes U+FFFD, which has three bytes, so the text can have up to three times the limit. In a URL-encoded body that is not UTF-8, a raw byte that is not ASCII becomes a Latin-1 character of two bytes. For example, a limit of `17` accepts 17 bytes `%FF` and gives a text of 51 bytes. |
| `DJANGO_BOLT_MAX_SYNC_STREAMING_THREADS` | int, 1 or more | `1000` | Wins over `BOLT_MAX_SYNC_STREAMING_THREADS`. |
| `DJANGO_BOLT_STREAM_SYNC_BATCH_SIZE` | int, 1 or more | `5` | Items that a sync generator sends in one batch. A server-sent event stream always sends 1. |
| `DJANGO_BOLT_STREAM_CHANNEL_CAPACITY` | int, 1 or more | `32` | Chunks that wait between a generator and the response. |

A parameter that is too long gives this response:

```json
{"detail": "Query parameter 'q': Parameter too long: 9000 bytes (max 8192 bytes)"}
```

### WebSocket

`DJANGO_BOLT_WS_MAX_CONNECTIONS`, `DJANGO_BOLT_WS_CHANNEL_SIZE`, `DJANGO_BOLT_WS_HEARTBEAT_INTERVAL`, `DJANGO_BOLT_WS_CLIENT_TIMEOUT` and `DJANGO_BOLT_WS_MAX_MESSAGE_SIZE` win over the `BOLT_WS_` settings with the same suffix.
See [WebSocket limits](#websocket-limits).

### Tests

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT` | number, more than 0 | `10` | Seconds that a thread of `TestClient` waits for the shared test database connection. |

See [Testing](../topics/testing.md).

## See also

- [runbolt](runbolt.md)
- [Deployment](../topics/deployment.md)
- [Logging](../topics/logging.md)

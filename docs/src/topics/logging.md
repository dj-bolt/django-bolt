---
icon: lucide/scroll-text
---

# Logging

Bolt logs each request in the Rust server: the access log of `runbolt`.
`LoggingConfig` sets the logger of the access log and the lines that it writes.
Bolt also logs the exceptions that give a 5xx response.

## Quick start

Give a `LoggingConfig` to the API:

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig

config = LoggingConfig(
    logger_name="myapp.api",
    skip_paths={"/health", "/metrics"},
    sample_rate=0.1,  # Log 10% of successful requests
)
api = BoltAPI(logging_config=config)
```

## LoggingConfig

`LoggingConfig` sets the access log and exception logging of an API.

### Basic options

```python
from django_bolt.logging import LoggingConfig

config = LoggingConfig(
    # Logger name (default: "django.server")
    logger_name="myapp.api",

    # Skip these paths (default includes health checks)
    skip_paths={"/health", "/ready", "/metrics"},

    # Skip these status codes
    skip_status_codes={204, 304},
)
```

### Logger name

The access log writes the lines of the routes of an API to the logger `logger_name`.
The default is `django.server`, the logger of Django's `runserver`.
Each API has its own logger, also after `mount()`.
`log_exception()` uses the same logger.

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig

api = BoltAPI(logging_config=LoggingConfig(logger_name="myapp.api"))
```

Set the level and the handlers of that logger in `LOGGING`:

```python
# settings.py
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "myapp.api": {"handlers": ["console"], "level": "INFO"},
    },
}
```

Bolt reads the level of each logger once, at startup. See [Access log](#access-log).

### Skip paths and status codes

Give the config to the API to apply `skip_paths` and `skip_status_codes` to the [access log](#access-log) of `runbolt`:

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig

api = BoltAPI(logging_config=LoggingConfig(skip_paths={"/health"}, skip_status_codes={204, 304}))
```

A request with a path in `skip_paths` writes no line. The match is exact, on the complete request path.
Thus `/health` does not skip `/health/` or `/api/health`. For a mounted API, give the path with the mount prefix.
A response with a status in `skip_status_codes` writes no line.
The two options apply to each status class, also to a 4xx and a 5xx.
An API with no `logging_config` has the default `skip_paths`: `/health`, `/ready` and `/metrics`.
To log these paths, use `LoggingConfig(skip_paths=set())`.

The access log of `runbolt` applies five options: `logger_name`, `skip_paths`, `skip_status_codes`, `sample_rate` and `min_duration_ms`.
Its line always contains the method, the path, the status and the duration.
To log more fields of a request, use a [middleware](middleware.md) of your own.

## Production settings

For production, use sampling and a duration threshold to make the access log of `runbolt` smaller.
Give the config to the API:

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig

config = LoggingConfig(
    # Only log 5% of successful requests
    sample_rate=0.05,

    # Only log requests taking 250ms or longer
    min_duration_ms=250,
)
api = BoltAPI(logging_config=config)
```

The two options apply to each route of that API, also after `mount()` and in a project with more than one API.
Bolt reads them once, when the API registers its routes. The Rust server applies them without a call to Python.
A response must pass the two checks to get its line.

**Important**: Errors (4xx, 5xx) are always logged regardless of sampling or duration settings.

An API with no `logging_config` takes the two values from the Django settings:

```python
# settings.py
DJANGO_BOLT_LOG_SAMPLE = 0.05   # sample_rate
DJANGO_BOLT_LOG_SLOW_MS = 250   # min_duration_ms
```

Without the settings, the access log has no limit.
A `sample_rate` out of 0.0 to 1.0, or a negative `min_duration_ms`, stops startup.
The level of the logger of the API applies first. See [Access log](#access-log).

### Sample rate

`sample_rate` controls what percentage of 1xx to 3xx responses are logged:

- `1.0` or `None` = Log all requests (default)
- `0.1` = Log 10% of successful requests
- `0.0` = Don't log any successful requests

```python
# Log only 5% of successful requests
config = LoggingConfig(sample_rate=0.05)
```

### Minimum duration

`min_duration_ms` skips logging fast successful requests:

```python
# Only log requests taking 250ms or longer
config = LoggingConfig(min_duration_ms=250)
```

A request that completes in 100ms won't be logged unless it's an error.

## LoggingMiddleware

Each API with logging has a `LoggingMiddleware`. It holds the `LoggingConfig` and its logger.

### Exception logging

```python
from django_bolt.logging import LoggingConfig, LoggingMiddleware

middleware = LoggingMiddleware(LoggingConfig(logger_name="myapp.api"))

try:
    # handler code
    pass
except Exception as e:
    middleware.log_exception(
        request={"method": "GET", "path": "/api/error"},
        exc=e,
        exc_info=True,  # Include traceback
    )
```

Bolt calls `log_exception` for an exception that a handler does not handle, but only when the default response for the exception is a 5xx. A 4xx, such as a validation error, `PermissionDenied` or `Http404`, is the fault of the client. The access log of `runbolt` records it at `WARNING`. This also applies to a validation error that handler code raises, because Bolt answers it with a 422. An `error_formatter` can change the status of the response, but not this choice. Thus a formatter cannot hide a server error from the log. An `HTTPException` is a planned response, so Bolt does not call `log_exception` for it. When Bolt cannot build the error response, it logs that error too.

### Custom exception handler

```python
def my_exception_handler(logger, request, exception, exc_info):
    # Send to error tracking service
    sentry_sdk.capture_exception(exception)
    # Still log it
    logger.error(f"Exception: {exception}", exc_info=exc_info)

config = LoggingConfig(
    exception_logging_handler=my_exception_handler
)
```

Bolt calls the handler only when the default response for the exception is a 5xx, as it does `log_exception`.

## Queue-based logging

Django-Bolt uses queue-based logging to prevent I/O blocking:

```python
from django_bolt.logging.config import setup_django_logging

# Called automatically by runbolt
setup_django_logging()
```

This creates a `QueueHandler` that offloads log writing to a background thread, ensuring log I/O doesn't block request handling.

## Log records of the Rust core

The Rust core of Bolt writes its own log records to stderr, one line each, without Python.
An example is an error that a mounted ASGI app raises:

```text
[django-bolt] ERROR bolt_asgi::asgi_http: ASGI mount: coroutine raised an exception: RuntimeError: boom
```

The effective level of the `django_bolt` logger sets the level of these records.
Bolt reads the level once, when `runbolt` starts the server or a test client starts.
A later change of the level does not apply to the Rust records.
A record of a lower level costs one check in Rust and is not formatted.
Without a `LOGGING` setting, Bolt sets this level to `DEBUG` when `DEBUG = True`, and to `WARNING` when `DEBUG = False`.
With your own `LOGGING`, the level of `django_bolt` applies.
If `LOGGING` does not set it, Python uses the level of the root logger.

Other crates, such as Actix, write only their `WARNING` and `ERROR` records.
Bolt escapes each line break in a message, such as `\n` or U+2028, so each record stays on one line.

To see the debug records of the Rust core, set the level of `django_bolt` to `DEBUG`:

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {"django_bolt": {"level": "DEBUG"}},
}
```

To turn the Rust records off, set the level to `CRITICAL`.
Handlers and formatters of `LOGGING` do not get these records.

## Django integration

### With Django's LOGGING setting

If you have a custom `LOGGING` configuration in `settings.py`, Django-Bolt respects it:

```python
# settings.py
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
        },
    },
}
```

### Without custom LOGGING

If no `LOGGING` is configured, Django-Bolt sets up sensible defaults with queue-based handlers.

### Access log

The Rust server writes one line for each response from a matched route to the logger of its API.
That is `django.server`, or the `logger_name` of the `LoggingConfig` of the API:

```text
GET /items/7 200 0.4ms
```

This includes a response that Bolt gives before the handler runs.
Examples are a 401 or a 403 from authentication or a guard, a 429 from a rate limit, and a 422 or a 400 from request validation.
Bolt also writes a line for three answers about a route path: the 308 redirect to the other slash form, the automatic `OPTIONS` answer, and the 405 for a method that the path does not have.
With an `error_formatter`, the line has the status of the formatted response.

Bolt also writes a line for the 404 of a path with no route.
The line goes to the logger of the API.
It uses the `skip_paths`, `skip_status_codes` and `sample_rate` of the API.
When `runbolt` serves more than one API, it uses the config of the first API.
Bolt takes the GIL to write each 404 line, and the limit of 429 lines below does not apply to it.
Thus a scanner that sends many requests to paths with no route costs a GIL call for each request.
To write no 404 line, add 404 to `skip_status_codes`. The filter runs in Rust, before the GIL.

Bolt writes no line for a mounted ASGI app or MCP server, for a static or media file, and for a [native route](native-routes.md).

A flood of 429 responses does not fill the log.
Each worker thread writes one line to each logger for the first 429 response of each second.
The next 429 line of that worker and logger has the number of 429 responses that wrote no line:

```text
GET /items/7 429 0.1ms | suppressed: 41
```

The limit applies to each 429 response of a route, from a rate limit and from a handler.
The rate limiter also writes its own line to stderr, one for each second and bucket.

The log level follows the status code, the same as Django's `runserver`:

| Status | Level |
|---|---|
| 1xx–3xx | `INFO` |
| 4xx | `WARNING` |
| 5xx | `ERROR` |

Set the logger to `WARNING` to keep only the 4xx and 5xx lines. Set it to `ERROR` to keep only the 5xx lines. Set it above `ERROR` to remove all its lines.
Bolt reads the level of each logger once, at startup.
When no route writes to a logger with a level of `ERROR` or lower, the access log is off.
Then the request path has no logging code.

`skip_paths` and `skip_status_codes` remove the lines of some paths and status codes. See [Basic options](#basic-options).
`sample_rate` and `min_duration_ms` remove lines of 1xx to 3xx responses. See [Production settings](#production-settings).
This includes the 308 redirect and the automatic `OPTIONS` answer of a route path.
The 308 redirect uses the values of the route at the other slash form.
The automatic `OPTIONS` answer uses the values of the route for the first method in its `Allow` header.
That method is the first of `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD` and `QUERY` that the path has.
The logger name also comes from that route.

## Example: Complete production setup

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig

# Production logging config
config = LoggingConfig(
    # Performance: Don't log every request
    sample_rate=0.1,          # Log 10% of successful requests
    min_duration_ms=100,      # Only log requests > 100ms

    # Skip noisy paths
    skip_paths={"/health", "/ready", "/metrics", "/favicon.ico"},
    skip_status_codes={204, 304},
)

api = BoltAPI(logging_config=config)
```

## Checking log configuration

```python
config = LoggingConfig()

# Check if a path should be logged
config.should_log_request("/api/users")       # True
config.should_log_request("/health")          # False

# Check with status code
config.should_log_request("/api/users", 204)  # False if 204 in skip_status_codes

# Get the logger
logger = config.get_logger()
```

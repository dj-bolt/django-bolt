---
description: Configure Django-Bolt logs with Django's LOGGING setting, read and filter the access log, log server errors, and write JSON logs.
---

# Logging

Django-Bolt logs through the Python `logging` module, so Django's `LOGGING` setting controls it.
Bolt writes to these loggers:

| Logger | Records |
| --- | --- |
| `django.server` | One access-log line for each response of a route. The traceback of each server error. |
| `django_bolt` and its children | Warnings and errors of Bolt, for example `django_bolt.error_handlers` for each server error. |
| `django_bolt` (level only) | The level of this logger also sets the records of the Rust core. See [Logs of the Rust core](#logs-of-the-rust-core). |

## Configure logging for production

Define `LOGGING` in your settings. This configuration writes each record to the console, and it keeps the access log:

```python title="blog/settings.py"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
```

With your own `LOGGING`, Bolt does not change the logging configuration.
Under systemd or Docker, the console output goes to the journal or to the container log.

```text
INFO 2026-11-21 20:13:44,165 django.server GET /articles 200 0.3ms
WARNING 2026-11-21 20:13:44,171 blog.api Search index is out of date
INFO 2026-11-21 20:13:44,171 django.server GET /articles/2 200 0.3ms
```

The `blog.api` line comes from `logging.getLogger(__name__).warning(...)` in a handler of `blog/api.py`.

!!! warning "Define LOGGING when you run more than one process"
    Without `LOGGING`, Bolt sends records to a queue that a thread of the `runbolt` supervisor reads. A forked worker has no such thread. With `--processes` above 1, the warnings and errors of your code and of `django_bolt` do not appear.

## Read the access log

The server writes one line to `django.server` for each response of a route:

```text
GET /articles 200 0.3ms
```

The line has the method, the path, the status and the time that the server took.
The level follows the status, as in `runserver`:

| Status | Level |
| --- | --- |
| 1xx to 3xx | `INFO` |
| 4xx | `WARNING` |
| 5xx | `ERROR` |

The access log includes the responses that Bolt sends before the handler runs. Examples are a `401` from authentication, a `429` from a rate limit and a `422` from validation.
It also includes the redirect to the other slash form of a path, the automatic `OPTIONS` response, and a `405`.

The access log has no line for:

- a request that matches no route (`404`),
- a static or a media file,
- a mounted ASGI app or an MCP server,
- a [native route](native-routes.md),
- a request from `TestClient`.

A flood of `429` responses writes one line for each second on each worker thread. The next line counts the others:

```text
GET /articles 429 0.1ms | suppressed: 41
```

## Make the access log smaller

### Change the level

The server reads the level of `django.server` one time, at startup.

- Set `"level": "WARNING"` to keep only the 4xx and 5xx lines.
- Set `"level": "ERROR"` to keep only the 5xx lines.
- Set `"level": "CRITICAL"` to turn the access log off. The server then does no logging work for a request.

### Skip paths and status codes

Give a `LoggingConfig` to the API:

```python title="blog/api.py"
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig

api = BoltAPI(
    logging_config=LoggingConfig(
        skip_paths={"/health", "/ready", "/metrics", "/favicon.ico"},
        skip_status_codes={204, 304},
    ),
)
```

- A request whose path is in `skip_paths` writes no line. The match is exact: `/health` does not skip `/health/` or `/api/health`.
- A response whose status is in `skip_status_codes` writes no line, also for a 4xx or a 5xx.
- An API with no `logging_config` skips `/health`, `/ready` and `/metrics`. To log them, give `LoggingConfig(skip_paths=set())`.

### Sample fast responses

Two options remove lines of 1xx to 3xx responses. A 4xx or a 5xx response always writes its line.

```python title="blog/api.py"
api = BoltAPI(
    logging_config=LoggingConfig(
        sample_rate=0.05,  # keep 5% of the 1xx to 3xx lines
        min_duration_ms=250,  # keep only responses that took 250 ms or more
    ),
)
```

A response must pass both checks to get its line. The Rust server applies them without a call to Python.

For an API with no `logging_config`, set the same values in the Django settings:

```python title="blog/settings.py"
DJANGO_BOLT_LOG_SAMPLE = 0.05
DJANGO_BOLT_LOG_SLOW_MS = 250
```

These two names are Django settings, not environment variables.
A `sample_rate` outside 0.0 to 1.0, or a negative `min_duration_ms`, stops startup.

`BoltAPI(enable_logging=False)` does not turn off the access log. It removes the default `skip_paths` and the exception log of [Log server errors](#log-server-errors).

## Log server errors

When a handler raises an exception that gives a 5xx response, Bolt writes two records with the traceback:

- `django.server` at `ERROR`: `Exception in GET /articles/1: RuntimeError: database is down`
- `django_bolt.error_handlers` at `ERROR`: `Internal Server Error: RuntimeError - database is down`

An exception that gives a 4xx response, such as a validation error, `PermissionDenied` or `Http404`, writes no traceback. The access line at `WARNING` records it.
An `HTTPException` is a planned response, so Bolt writes no traceback for it.

To send server errors to an error tracker, give an `exception_logging_handler`.
Bolt calls it in place of the `django.server` record:

```python title="blog/api.py"
import logging

from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig


def report_exception(logger: logging.Logger, request, exc: Exception, exc_info: bool) -> None:
    # Send the exception to your error tracker here.
    logger.error("Unhandled error in %s %s", request.method, request.path, exc_info=exc_info)


api = BoltAPI(logging_config=LoggingConfig(exception_logging_handler=report_exception))


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    raise RuntimeError("database is down")
```

```bash
curl localhost:8000/articles/1
```

```json
{"detail": "Internal Server Error"}
```

With the `LOGGING` of [Configure logging for production](#configure-logging-for-production), the console shows three records:

```text
ERROR 2026-11-21 20:13:44,177 django.server Unhandled error in GET /articles/1
Traceback (most recent call last):
  ...
RuntimeError: database is down
ERROR 2026-11-21 20:13:44,180 django_bolt.error_handlers Internal Server Error: RuntimeError - database is down
Traceback (most recent call last):
  ...
RuntimeError: database is down
ERROR 2026-11-21 20:13:44,181 django.server GET /articles/1 500 3.6ms
```

Without a handler, `error_log_level` sets the level of the `django.server` record. The default is `"ERROR"`.

## Write JSON logs

A log collector reads one JSON object for each line. Add a formatter class to your project:

```python title="blog/logformat.py"
import json
import logging


class JSONFormatter(logging.Formatter):
    """Write each record as one JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)
```

Use it in `LOGGING`:

```python title="blog/settings.py"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "blog.logformat.JSONFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
```

```text
{"time": "2026-11-21T20:01:20", "level": "INFO", "logger": "django.server", "message": "GET /articles 200 0.3ms"}
{"time": "2026-11-21T20:01:20", "level": "WARNING", "logger": "blog", "message": "app warning from handler"}
{"time": "2026-11-21T20:01:20", "level": "ERROR", "logger": "django.server", "message": "GET /boom 500 3.6ms"}
```

An access-log record has only a message. It has no extra fields for the method or the status.

## Logs of the Rust core

The Rust core writes its own records to stderr, one line each. They do not go through the handlers of `LOGGING`:

```text
[django-bolt] ERROR bolt_asgi::asgi_http: ASGI mount: coroutine raised an exception: RuntimeError: boom
```

The effective level of the `django_bolt` logger sets which records appear. Bolt reads it one time, at startup.
Set `"django_bolt": {"level": "DEBUG"}` in `loggers` to see the debug records. Set `CRITICAL` to turn them off.
Records of other Rust crates, such as Actix, appear only at `WARNING` and above.

Some startup warnings, for example about a missing static directory, go to stderr at each level.

## Logging without a LOGGING setting

When your settings module has no `LOGGING`, `runbolt` configures logging at startup:

- The root logger, `django`, `django.server` and `django_bolt` write to a queue. A background thread writes the queue to stderr.
- The format is `INFO - 2026-11-21 20:00:28 - django.server - GET /articles 200 0.2ms`.
- The level is `DEBUG` with `DEBUG = True`, and `WARNING` with `DEBUG = False`. `django.server` keeps the `INFO` level of Django, so the access log stays on.
- When the queue holds `BOLT_LOG_QUEUE_SIZE` records (10000 by default), Bolt drops new records and prints `django-bolt: log queue saturated, dropped N record(s)`.

When `runbolt` serves the Django admin, Django sets up its own logging again. The access log then uses Django's format: `[21/Nov/2026 19:59:46,729] GET /articles 200 0.3ms`.

## LoggingConfig options

`LoggingConfig` is a dataclass in `django_bolt.logging`.

| Option | Default | Effect |
| --- | --- | --- |
| `skip_paths` | `{"/health", "/ready", "/metrics"}` | Paths with no access-log line. |
| `skip_status_codes` | `set()` | Status codes with no access-log line. |
| `sample_rate` | `None` | Part of the 1xx to 3xx lines to keep, from 0.0 to 1.0. `None` keeps each line. |
| `min_duration_ms` | `None` | Keep a 1xx to 3xx line only when the response took this time or more. |
| `exception_logging_handler` | `None` | Callable `(logger, request, exc, exc_info)` for each server error. |
| `error_log_level` | `"ERROR"` | Level of the server-error record when there is no handler. |
| `logger_name` | `"django.server"` | Logger of the server-error record. The access log always uses `django.server`. |
| `request_log_fields` | `{"method", "path", "status_code"}` | Fields of `LoggingMiddleware.log_request()`. |
| `response_log_fields` | `{"status_code"}` | Fields of `LoggingMiddleware.log_response()`. |
| `obfuscate_headers` | `{"authorization", "cookie", "x-api-key", "x-auth-token"}` | Headers that `log_request()` writes as `***`. |
| `obfuscate_cookies` | `{"sessionid", "csrftoken"}` | Cookies that `obfuscate_cookies()` writes as `***`. |
| `log_request_body` | `False` | Let `log_request()` write the body when `request_log_fields` has `"body"`. |
| `max_body_log_size` | `1024` | Largest body in bytes that `log_request()` writes. |

Bolt calls only the exception log of `LoggingMiddleware`. The field and body options apply when your own code calls `log_request()` or `log_response()`. They do not change the access log.

## See also

- [Settings reference](../ref/settings.md#logging)
- [Deployment](deployment.md)
- [Error handling](error-handling.md)
- [Health checks](health-checks.md)

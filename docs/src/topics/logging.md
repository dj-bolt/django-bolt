---
icon: lucide/scroll-text
---

# Logging

Django-Bolt provides a comprehensive logging system with queue-based non-blocking logging, configurable log levels, request/response logging, and header obfuscation.

## Quick start

Create a logging middleware with default settings:

```python
from django_bolt.logging import create_logging_middleware

middleware = create_logging_middleware()
```

Or configure it with custom options:

```python
from django_bolt.logging import LoggingConfig, LoggingMiddleware

config = LoggingConfig(
    logger_name="myapp.api",
    skip_paths={"/health", "/metrics"},
    sample_rate=0.1,  # Log 10% of successful requests
)
middleware = LoggingMiddleware(config)
```

## LoggingConfig

The `LoggingConfig` class controls all logging behavior.

### Basic options

```python
from django_bolt.logging import LoggingConfig

config = LoggingConfig(
    # Logger name (default: "django.server")
    logger_name="myapp.api",

    # Log level (default: "INFO")
    log_level="DEBUG",

    # Skip these paths (default includes health checks)
    skip_paths={"/health", "/ready", "/metrics"},

    # Skip these status codes
    skip_status_codes={204, 304},
)
```

### Request logging fields

Control which request fields are logged:

```python
config = LoggingConfig(
    request_log_fields={
        "method",      # HTTP method
        "path",        # Request path
        "query",       # Query parameters
        "headers",     # Request headers (obfuscated)
        "body",        # Request body
        "client_ip",   # Client IP address
        "user_agent",  # User agent string
    }
)
```

Default: `{"method", "path", "status_code"}`

### Response logging fields

Control which response fields are logged:

```python
config = LoggingConfig(
    response_log_fields={
        "status_code",  # HTTP status code
        "duration",     # Response time in ms
        "size",         # Response size in bytes
    }
)
```

### Security: Header obfuscation

Sensitive headers are automatically obfuscated:

```python
config = LoggingConfig(
    obfuscate_headers={
        "authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
    }
)
```

These headers appear as `"***"` in logs:

```
POST /api/users - headers: {"authorization": "***", "content-type": "application/json"}
```

### Security: Cookie obfuscation

Sensitive cookies are also obfuscated:

```python
config = LoggingConfig(
    obfuscate_cookies={
        "sessionid",
        "csrftoken",
    }
)
```

### Request body logging

Enable request body logging with size limits:

```python
config = LoggingConfig(
    log_request_body=True,
    max_body_log_size=1000,  # Skip bodies larger than 1KB
)
```

Binary data is logged as size information:

```
POST /api/upload - body: "<binary data, 1024 bytes>"
```

## Production settings

For production, use sampling and duration thresholds to reduce log volume:

```python
config = LoggingConfig(
    # Only log 5% of successful requests
    sample_rate=0.05,

    # Only log requests taking longer than 250ms
    min_duration_ms=250,
)
```

**Important**: Errors (4xx, 5xx) are always logged regardless of sampling or duration settings.

### Sample rate

`sample_rate` controls what percentage of 2xx/3xx responses are logged:

- `1.0` = Log all requests (default)
- `0.1` = Log 10% of successful requests
- `0.0` = Don't log any successful requests

```python
# Log only 5% of successful requests
config = LoggingConfig(sample_rate=0.05)
```

### Minimum duration

`min_duration_ms` skips logging fast successful requests:

```python
# Only log requests taking > 250ms
config = LoggingConfig(min_duration_ms=250)
```

A request that completes in 100ms won't be logged unless it's an error.

## Log levels

Django-Bolt uses appropriate log levels based on status codes:

| Status Code | Log Level | Description |
|-------------|-----------|-------------|
| 2xx | INFO | Successful requests |
| 3xx | INFO | Redirects |
| 4xx | WARNING | Client errors |
| 5xx | ERROR | Server errors |

Requests are logged at DEBUG level (before processing). Responses are logged at the level above.

## LoggingMiddleware

### Using with handlers

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig, LoggingMiddleware

config = LoggingConfig(
    logger_name="myapp.api",
    skip_paths={"/health"},
)
middleware = LoggingMiddleware(config)

# Log request
middleware.log_request({
    "method": "POST",
    "path": "/api/users",
    "headers": {"content-type": "application/json"},
})

# Log response
middleware.log_response(
    request={"method": "POST", "path": "/api/users"},
    status_code=201,
    duration=0.05,  # seconds
)
```

### Exception logging

```python
try:
    # handler code
    pass
except Exception as e:
    middleware.log_exception(
        request={"method": "GET", "path": "/api/error"},
        exception=e,
        exc_info=True,  # Include traceback
    )
```

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

## Client IP extraction

The middleware extracts client IP from proxy headers:

1. `X-Forwarded-For` (first IP in chain)
2. `X-Real-IP`
3. Falls back to connection IP

```python
config = LoggingConfig(
    request_log_fields={"method", "path", "client_ip"}
)

# Logs: GET /api/users - client_ip: 192.168.1.1
```

## Queue-based logging

Django-Bolt uses queue-based logging to prevent I/O blocking:

```python
from django_bolt.logging.config import setup_django_logging

# Called automatically by runbolt
setup_django_logging()
```

This creates a `QueueHandler` that offloads log writing to a background thread, ensuring log I/O doesn't block request handling.

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

The Rust server writes one line for each response from a matched route to the `django.server` logger:

```text
GET /items/7 200 0.4ms
```

The log level follows the status code, the same as Django's `runserver`:

| Status | Level |
|---|---|
| 1xx–3xx | `INFO` |
| 4xx | `WARNING` |
| 5xx | `ERROR` |

Set `django.server` to `WARNING` to keep only the 4xx and 5xx lines. Set it above `WARNING` to turn the access log off. The check runs once at startup. When the log is off, the request path has no logging code.

## Example: Complete production setup

```python
from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig, create_logging_middleware

api = BoltAPI()

# Production logging config
config = LoggingConfig(
    logger_name="myapp.api",

    # Performance: Don't log every request
    sample_rate=0.1,          # Log 10% of successful requests
    min_duration_ms=100,      # Only log requests > 100ms

    # Skip noisy paths
    skip_paths={"/health", "/ready", "/metrics", "/favicon.ico"},
    skip_status_codes={204, 304},

    # Request logging
    request_log_fields={"method", "path", "client_ip", "user_agent"},

    # Response logging
    response_log_fields={"status_code", "duration"},

    # Security
    obfuscate_headers={"authorization", "cookie", "x-api-key"},
    obfuscate_cookies={"sessionid", "csrftoken", "jwt"},

    # Body logging (careful in production)
    log_request_body=False,
)

middleware = create_logging_middleware(
    logger_name=config.logger_name,
    skip_paths=config.skip_paths,
    sample_rate=config.sample_rate,
)
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

---
icon: lucide/settings
---

# Settings Reference

Django-Bolt settings are configured in your Django `settings.py` file.

## CORS settings

### CORS_ALLOWED_ORIGINS

List of allowed origins for CORS.

```python
CORS_ALLOWED_ORIGINS = [
    "https://example.com",
    "https://app.example.com",
]
```

### CORS_ALLOW_ALL_ORIGINS

Allow all origins (development only).

```python
CORS_ALLOW_ALL_ORIGINS = True
```

### CORS_ALLOW_CREDENTIALS

Allow credentials in CORS requests.

```python
CORS_ALLOW_CREDENTIALS = True
```

### CORS_ALLOW_METHODS

Allowed HTTP methods for CORS.

```python
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
```

### CORS_ALLOW_HEADERS

Allowed headers in CORS requests.

```python
CORS_ALLOW_HEADERS = ["Content-Type", "Authorization", "X-Requested-With"]
```

### CORS_EXPOSE_HEADERS

Headers exposed to the browser.

```python
CORS_EXPOSE_HEADERS = ["X-Total-Count", "X-Page-Count"]
```

### CORS_MAX_AGE

Preflight cache duration in seconds.

```python
CORS_MAX_AGE = 86400  # 24 hours
```

## File upload settings

### BOLT_MAX_UPLOAD_SIZE

Maximum file upload size in bytes. Requests exceeding this limit will be rejected with a 413 error **before** any per-view validation occurs.

```python
BOLT_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
```

You can also use the `FileSize` enum for readability:

```python
from django_bolt import FileSize

BOLT_MAX_UPLOAD_SIZE = FileSize.MB_10
```

!!! warning "Global vs Per-View Limits"
    `BOLT_MAX_UPLOAD_SIZE` is a **global server limit**. You cannot override it in individual views. If a file upload exceeds this value, the request is rejected before your view handler runs.

    The `max_size` argument in the `File()` parameter is for **per-view validation**. It allows you to set a stricter limit for a specific endpoint, but it can never increase the global limit. For example:
    ```python
    # settings.py
    BOLT_MAX_UPLOAD_SIZE = FileSize.MB_50  # 50 MB global limit

    # In your view
    file: Annotated[UploadFile, File(max_size=FileSize.MB_1)]  # 1 MB per-view limit
    ```
    In this case, files over 1 MB are rejected by the view, and files over 50 MB are rejected by the server.

    **If you set `File(max_size=...)` higher than `BOLT_MAX_UPLOAD_SIZE`, the global limit always takes precedence.**

    This distinction is important for security and resource management. Always set `BOLT_MAX_UPLOAD_SIZE` to the maximum file size your server should ever accept, and use `File(max_size=...)` for endpoint-specific needs.

## ASGI mount settings

### BOLT_ASGI_MOUNT_TIMEOUT

Maximum time (seconds) to wait for a mounted ASGI app to complete.

```python
BOLT_ASGI_MOUNT_TIMEOUT = 30
```

**Default:** `30`

When exceeded, Django-Bolt returns `504 Gateway Timeout` for the mounted request.

### BOLT_MEMORY_SPOOL_THRESHOLD

Size threshold before file uploads are spooled to disk. Files smaller than this are kept in memory; larger files are written to a temporary file on disk.

```python
BOLT_MEMORY_SPOOL_THRESHOLD = 5 * 1024 * 1024  # 5 MB
```

**Default:** `1048576` (1 MB)

This setting controls memory usage during file uploads:

- **Lower values** reduce memory usage but increase disk I/O
- **Higher values** improve performance for medium-sized files but use more memory

!!! tip "When to adjust"
    - Set higher (e.g., 5-10 MB) if you frequently receive medium-sized files and have sufficient memory
    - Set lower (e.g., 256 KB) on memory-constrained systems or when handling many concurrent uploads

## File serving settings

### BOLT_ALLOWED_FILE_PATHS

Whitelist of directories for FileResponse.

```python
BOLT_ALLOWED_FILE_PATHS = [
    "/var/app/uploads",
    "/var/app/public",
]
```

When set, `FileResponse` only serves files within these directories.

## Authentication settings

Django-Bolt uses Django's `SECRET_KEY` for JWT signing by default.

```python
SECRET_KEY = "your-secret-key"
```

Override per-backend:

```python
JWTAuthentication(secret="custom-jwt-secret")
```

### BOLT_AUTHENTICATION_CLASSES

Default authentication backends applied to all endpoints.

**Default:** `[]` (no authentication)

### BOLT_DEFAULT_PERMISSION_CLASSES

Default permission guards applied to all endpoints.

**Default:** `[AllowAny()]` (all endpoints publicly accessible)

See [Global authentication](../topics/authentication.md#global-authentication) for usage and examples.

## Logging settings

Django-Bolt integrates with Django's logging system.

```python
LOGGING = {
    "version": 1,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
        "django_bolt": {
            "handlers": ["console"],
            "level": "INFO",
        },
    },
}
```

## Django signals settings

### BOLT_EMIT_SIGNALS

Enable Django `request_started` and `request_finished` signal emission.

```python
BOLT_EMIT_SIGNALS = True
```

**Default:** `False`

Django-Bolt disables signals by default for maximum performance. Enable this setting when:

- Using `CONN_MAX_AGE` with a value other than `None` (required for connection recycling)
- Using third-party packages that depend on request signals (e.g., django-debug-toolbar)
- Implementing custom signal receivers for request lifecycle events

!!! warning "Required for CONN_MAX_AGE"
    If you set `CONN_MAX_AGE=600` (or any non-None value), you **must** enable signals for Django to properly close old connections:
    ```python
    CONN_MAX_AGE = 600
    BOLT_EMIT_SIGNALS = True  # Required!
    ```

See [Django Signals](../topics/signals.md) for detailed documentation.

## runbolt command options

The `runbolt` management command accepts these options:

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8000` | Bind port |
| `--workers` | `1` | Workers per process |
| `--processes` | `1` | Number of processes |
| `--dev` | off | Enable auto-reload |
| `--no-admin` | off | Disable admin integration |
| `--backlog` | `1024` | Socket listen backlog |
| `--keep-alive` | OS default | HTTP keep-alive timeout |

### Examples

```bash
# Development with auto-reload
python manage.py runbolt --dev

# Production with scaling
python manage.py runbolt --processes 4 --workers 2

# Custom bind address
python manage.py runbolt --host 127.0.0.1 --port 3000
```

## OpenAPI settings

Configure via `OpenAPIConfig` in your api.py:

```python
from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        description="API description",
        enabled=True,
        docs_url="/docs",
        openapi_url="/openapi.json",
        django_auth=False,
    )
)
```

## Content Security Policy (CSP) settings

Django-Bolt applies CSP headers to static files served by the Rust server using Django 6.0+'s native [`SECURE_CSP`](https://docs.djangoproject.com/en/6.0/ref/csp/) setting.

### SECURE_CSP

```python
from django.utils.csp import CSP

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
}
```

See [Static Files - Content Security Policy](../topics/static-files.md#content-security-policy-csp) for full documentation.

## Compression settings

Configure via `CompressionConfig`:

```python
from django_bolt import BoltAPI, CompressionConfig

api = BoltAPI(
    compression=CompressionConfig(
        backend="brotli",      # "brotli", "gzip", or "zstd"
        minimum_size=1000,     # Minimum size to compress (bytes)
        gzip_fallback=True,    # Fall back to gzip
    )
)
```

## All settings reference

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `CORS_ALLOWED_ORIGINS` | `list[str]` | `[]` | Allowed CORS origins |
| `CORS_ALLOW_ALL_ORIGINS` | `bool` | `False` | Allow all origins |
| `CORS_ALLOW_CREDENTIALS` | `bool` | `False` | Allow credentials |
| `CORS_ALLOW_METHODS` | `list[str]` | All methods | Allowed methods |
| `CORS_ALLOW_HEADERS` | `list[str]` | `[]` | Allowed headers |
| `CORS_EXPOSE_HEADERS` | `list[str]` | `[]` | Exposed headers |
| `CORS_MAX_AGE` | `int` | `600` | Preflight cache (seconds) |
| `BOLT_MAX_UPLOAD_SIZE` | `int` | `1048576` | Max upload size (bytes) |
| `BOLT_MEMORY_SPOOL_THRESHOLD` | `int` | `1048576` | Memory threshold before disk spooling (bytes) |
| `BOLT_ALLOWED_FILE_PATHS` | `list[str]` | `None` | File serving whitelist |
| `BOLT_EMIT_SIGNALS` | `bool` | `False` | Enable Django request signals |
| `SECURE_CSP` | `dict` | `None` | CSP directives for static files ([Django 6.0+](https://docs.djangoproject.com/en/6.0/ref/csp/)) |
| `BOLT_AUTHENTICATION_CLASSES` | `list` | `[]` | Default authentication backends |
| `BOLT_DEFAULT_PERMISSION_CLASSES` | `list` | `[AllowAny()]` | Default permission guards |

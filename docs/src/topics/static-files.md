---
icon: lucide/file-image
---

# Static Files

For production deployments, we recommend letting **Nginx** handle static files at the reverse proxy layer — this offloads static traffic entirely so Django-Bolt focuses on API requests. That said, Django-Bolt also serves static files directly from Rust using Actix-files, delivering high-performance static file serving without Python overhead — a solid option when you want a simpler setup without configuring static file handling in your reverse proxy.

!!! tip "Serving user uploads?"
    This page covers **static assets** (CSS, JS, admin) from `STATIC_ROOT` / `STATICFILES_DIRS`. For user-uploaded media (`MEDIA_ROOT`), see [Media Files](media-files.md) — it shares this handler but applies stricter, upload-specific defaults.

## Overview

Static files are served with:

- **Streaming responses** - Memory efficient for large files
- **ETag and Last-Modified headers** - Automatic caching support
- **Conditional requests** - 304 Not Modified responses
- **Range requests** - Resumable downloads
- **Content-Type detection** - Automatic MIME type handling
- **Content Security Policy** - Configurable CSP headers

## Configuration

Django-Bolt reads your existing Django static files settings:

```python
# settings.py

# URL prefix for static files
STATIC_URL = "/static/"

# Directory where collectstatic gathers files (primary lookup)
STATIC_ROOT = BASE_DIR / "staticfiles"

# Additional directories to search (optional)
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
```

## File lookup order

When a static file is requested, Django-Bolt searches in this order:

1. **STATIC_ROOT** - Collected static files (fastest)
2. **STATICFILES_DIRS** - Additional configured directories
3. **Django staticfiles finders** (debug mode only) - App-level static files (e.g., admin)

The first match is returned. Files in `STATIC_ROOT` take priority.

!!! note "Finders disabled in production"
    For security, Django's staticfiles finders fallback is only enabled when `DEBUG=True`. In production, only files in `STATIC_ROOT` and `STATICFILES_DIRS` are served. Run `collectstatic` to copy app-level static files (like admin assets) into `STATIC_ROOT`.

## Django admin integration

In development (`DEBUG=True`), Django admin static files are automatically served via Django's staticfiles finders — no extra configuration needed.

In production (`DEBUG=False`), you must run `collectstatic` so admin assets are copied into `STATIC_ROOT`:

```bash
python manage.py collectstatic
```

```python
# Admin works out of the box in development
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.staticfiles",
    # ...
]
```

## Security

### Directory traversal protection

Django-Bolt blocks directory traversal attacks:

- Paths containing `..` are rejected with `400`
- Symlink targets are verified to stay within allowed directories
- Only files (not directories) are served

### Dotfile deny

Any request whose path has a leading-dot component — `.env`, `.git/config`, `.htaccess`, `.ssh/id_rsa` — returns `404`, matching the nginx/Apache default. This keeps stray dotfiles left in `STATIC_ROOT` from being reachable over HTTP. Backslash-separated components (`foo\.env`) are caught as well.

### nosniff

`X-Content-Type-Options: nosniff` is applied to **every** response, including 404s, so a crafted miss can't be MIME-sniffed into HTML/JS execution.

!!! note "User uploads need more"
    Static files keep their native content types because they're admin-curated. **Media** uploads are untrusted, so the media handler additionally force-downloads script-bearing files (`.html`, `.svg`, `.js`, …). See [Media Files — Security model](media-files.md#security-model).

### Content Security Policy (CSP)

Django-Bolt applies CSP headers to all static file responses. This protects against XSS attacks in HTML files and ensures scripts/styles are loaded only from trusted sources.

Configure CSP using Django 6.0+'s native [`SECURE_CSP`](https://docs.djangoproject.com/en/6.0/ref/csp/) setting:

```python
# settings.py
from django.utils.csp import CSP

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
}
```

You can also use raw strings:

```python
SECURE_CSP = {
    "default-src": ["'self'"],
    "script-src": ["'self'"],
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:"],
}
```

#### CSP constants

Django provides constants via `django.utils.csp.CSP`:

| Constant | Value | Purpose |
|----------|-------|---------|
| `CSP.SELF` | `'self'` | Same origin only |
| `CSP.NONE` | `'none'` | Block all resources |
| `CSP.UNSAFE_INLINE` | `'unsafe-inline'` | Allow inline scripts/styles |
| `CSP.UNSAFE_EVAL` | `'unsafe-eval'` | Allow `eval()` |
| `CSP.STRICT_DYNAMIC` | `'strict-dynamic'` | Trust scripts loaded by trusted scripts |

#### How CSP works

- CSP configuration is read once at server startup
- The header is pre-built for performance (no per-request overhead)
- `CSP.NONCE` values are automatically filtered out (static files cannot inject nonces)

!!! note "Nonces not supported"
    `CSP.NONCE` requires per-request nonce injection which isn't possible for static files. Use `CSP.NONCE` only for dynamic responses served by your Django views.

#### Common CSP directives

| Directive | Purpose | Example |
|-----------|---------|---------|
| `default-src` | Default policy for all content | `[CSP.SELF]` |
| `script-src` | JavaScript sources | `[CSP.SELF, "https://cdn.example.com"]` |
| `style-src` | CSS sources | `[CSP.SELF, CSP.UNSAFE_INLINE]` |
| `img-src` | Image sources | `[CSP.SELF, "data:", "https:"]` |
| `font-src` | Font sources | `[CSP.SELF, "https://fonts.gstatic.com"]` |
| `connect-src` | XHR/WebSocket destinations | `[CSP.SELF, "https://api.example.com"]` |
| `frame-ancestors` | Who can embed this page | `[CSP.NONE]` |
| `upgrade-insecure-requests` | Upgrade HTTP to HTTPS | `[]` (boolean directive) |
| `block-all-mixed-content` | Block mixed HTTP/HTTPS | `[]` (boolean directive) |

## Using with templates

The Django `{% static %}` template tag works as expected:

```html
{% load static %}
<!DOCTYPE html>
<html>
<head>
    <link rel="stylesheet" href="{% static 'css/style.css' %}">
</head>
<body>
    <img src="{% static 'img/logo.png' %}" alt="Logo">
    <script src="{% static 'js/app.js' %}"></script>
</body>
</html>
```

## Caching

Set `BOLT_STATIC_MAX_AGE` (seconds) to emit a `Cache-Control` header on successful static responses:

```python
# settings.py
BOLT_STATIC_MAX_AGE = 31536000  # 1 year — safe for content-hashed assets
```

This produces `Cache-Control: public, max-age=31536000`. Static uses **`public`** (unlike media's `private`) because assets are identical for every user, so CDNs and shared proxies can cache them aggressively.

- The header is only set on `2xx` responses — a `404` is never cached with a long `max-age`.
- A missing or unset `BOLT_STATIC_MAX_AGE` means no `Cache-Control` header — this is the default and emits no warning. Startup warnings are only emitted for present-but-invalid values (non-integer, boolean, or negative), which are ignored.
- This is independent of the ETag / Last-Modified validators, which are always sent.

## Performance

Static file serving runs primarily in Rust:

- **No Python GIL for configured directories** - Files in `STATIC_ROOT` and `STATICFILES_DIRS` are served without touching Python
- **Django finders fallback** (debug mode only) - App-level static files (like admin) require a Python call; disabled in production for security
- **Async I/O** - Non-blocking file reads via Actix
- **Smart read modes** - Sync reads for small files (<256KB), async for large files
- **Efficient caching** - Proper HTTP caching headers reduce server load


## Production deployment

For production, collect static files (including admin assets) into `STATIC_ROOT`:

```bash
python manage.py collectstatic
```

Then run Django-Bolt:

```bash
python manage.py runbolt --host 0.0.0.0 --port 8000 --processes 4
```

Static files are served from `STATIC_ROOT` with async I/O, ETag caching, and range request support.

## Troubleshooting

### Static files not found

1. Verify `STATIC_ROOT` is set and contains files:
   ```bash
   python manage.py collectstatic
   ls -la $(python -c "from django.conf import settings; print(settings.STATIC_ROOT)")
   ```

2. Check `STATIC_URL` matches your requests:
   ```python
   # settings.py
   STATIC_URL = "/static/"  # Requests should be /static/css/style.css
   ```

3. For development without collectstatic, ensure `STATICFILES_DIRS` is configured.

### Admin styles missing

Ensure `django.contrib.staticfiles` is in `INSTALLED_APPS` and run `collectstatic`:

```bash
python manage.py collectstatic --noinput
```

### CSP blocking resources

Check browser console for CSP violations. Adjust your CSP directives to allow required sources:

```python
from django.utils.csp import CSP

SECURE_CSP = {
    "script-src": [CSP.SELF, "https://trusted-cdn.com"],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],  # Allow inline styles
}
```

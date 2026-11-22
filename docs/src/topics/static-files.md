---
description: Serve CSS, JavaScript and admin assets from Django-Bolt with STATIC_URL, STATIC_ROOT and collectstatic, with cache headers for production.
---

# Static files

Django-Bolt serves static files from Rust, with no call to Python.
It reads the Django settings that you already have: `STATIC_URL`, `STATIC_ROOT` and `STATICFILES_DIRS`.

```python title="blog/settings.py"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
```

```bash
curl -i localhost:8000/static/css/site.css
```

```http
HTTP/1.1 200 OK
content-length: 23
last-modified: Sun, 22 Nov 2026 01:59:05 GMT
etag: "bc6d7:17:6b024c69:2ea3cb0b"
x-content-type-options: nosniff
content-type: text/css; charset=utf-8
accept-ranges: bytes
content-disposition: inline; filename="site.css"

body { color: black; }
```

The `{% static %}` template tag gives the same URLs, as in each Django project.
Bolt serves `GET` and `HEAD`. Static responses do not appear in the access log.

## How Bolt finds a file

For a request to `/static/css/site.css`, Bolt looks for `css/site.css` in this order:

1. `STATIC_ROOT`.
2. Each directory of `STATICFILES_DIRS`, in order. Bolt does not read a `(prefix, path)` entry. With `DEBUG = False`, run `collectstatic` to copy its files into `STATIC_ROOT`.
3. With `DEBUG = True` only: the Django staticfiles finders, for example the `static/` directory of each app.

The first file found wins. A request with no file gets `404` with the body `File not found`.

Bolt reads the settings one time, at startup. It ignores a directory that does not exist, and prints a warning:

```text
[django-bolt] Warning: Static files: cannot resolve directory for /static: /srv/blog/staticfiles (No such file or directory (os error 2))
```

## Serve static files in development

With `DEBUG = True`, the staticfiles finders serve the files of your apps and of the Django admin.
You do not need `collectstatic`. Add `django.contrib.staticfiles` to `INSTALLED_APPS`:

```python title="blog/settings.py"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.staticfiles",
    # ...
    "django_bolt",
]
```

```bash
curl -I localhost:8000/static/admin/css/base.css
```

```http
HTTP/1.1 200 OK
content-type: text/css; charset=utf-8
```

## Serve static files in production

With `DEBUG = False`, Bolt does not run the finders. Copy all static files into `STATIC_ROOT` before you start the server:

```bash
python manage.py collectstatic --noinput
```

```text
131 static files copied to '/srv/blog/staticfiles'.
```

Without `collectstatic`, the admin has no CSS, because its files are not in `STATIC_ROOT`:

```bash
curl -I localhost:8000/static/admin/css/base.css
```

```http
HTTP/1.1 404 Not Found
x-content-type-options: nosniff
```

When `STATIC_ROOT` and `STATICFILES_DIRS` give no directory and `DEBUG = False`, Bolt does not serve `STATIC_URL`. It prints this warning:

```text
[django-bolt] Warning: Static files: No valid directories found for /static and DEBUG=False — not serving /static. Run collectstatic to populate STATIC_ROOT.
```

A reverse proxy or a CDN can also serve `STATIC_ROOT`. Then the static requests never reach Bolt. See [Deployment](deployment.md#run-behind-a-reverse-proxy).

## Cache static files

Each response has an `ETag` and a `Last-Modified` header.
A browser that sends the `ETag` back in `If-None-Match` gets `304 Not Modified` with no body.
A `Range` request gets `206 Partial Content`.

To let browsers and CDNs keep the files, set `BOLT_STATIC_MAX_AGE` in seconds:

```python title="blog/settings.py"
BOLT_STATIC_MAX_AGE = 31536000  # 1 year
```

```http
HTTP/1.1 200 OK
cache-control: public, max-age=31536000
content-type: text/css; charset=utf-8
```

- Bolt adds `Cache-Control` only to a `2xx` response. A `404` is not cached.
- Without the setting, or with `None`, Bolt adds no `Cache-Control` header.
- A value that is not an `int` of 0 or more stops startup with `ImproperlyConfigured`.

A long `max-age` is safe only when the name of a file changes with its content.
Use `ManifestStaticFilesStorage`. `collectstatic` then writes copies with a hash in the name, such as `css/site.55e7cbb9ba48.css`, and `{% static %}` gives those names:

```python title="blog/settings.py"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
```

When compression is on, Bolt compresses a static file for a client that accepts it, as for each other response. See [Compression](compression.md).

## Add a Content-Security-Policy header

Set Django's `SECURE_CSP` setting. Bolt sends the policy on each static and media response:

```python title="blog/settings.py"
from django.utils.csp import CSP

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
}
```

```http
content-security-policy: default-src 'self'; img-src 'self' data:
```

- Bolt builds the header with the rules of Django. `True` gives a directive with no value, such as `"upgrade-insecure-requests": True`.
- Bolt removes `CSP.NONCE`, because a file has no nonce.
- A `SECURE_CSP` that is not a dict stops startup with `ImproperlyConfigured`.
- `django.utils.csp` exists in Django 6.0 and later. On Django 5.2, write the values as strings, such as `"'self'"`. Bolt sends the header on each version.

## Security

Bolt applies these rules to each static request:

- A path with `..` gets `400` with the body `Invalid path`.
- A path with a part that starts with a dot, such as `.env` or `.git/config`, gets `404`.
- A symbolic link that points out of the directory gives `404`. With `DEBUG = True`, the Django finders can still find the file of such a link. Do not expose a server with `DEBUG = True`.
- Each response, also a `404`, has `X-Content-Type-Options: nosniff`.

Bolt sends a static file with its own content type, also an `.html` or `.svg` file. Put only files that you trust in `STATIC_ROOT`. For files that users upload, see [Media files](media-files.md).

## Limits of STATIC_URL

- A `STATIC_URL` of `/` is not served. It would hide each route.
- A `STATIC_URL` with `{` or `}` is not served. Bolt prints a warning.
- A `STATIC_URL` that is a complete URL, such as `https://cdn.example.com/static/`, is not served by Bolt. The CDN serves it.

## See also

- [Media files](media-files.md)
- [Deployment](deployment.md)
- [Settings reference](../ref/settings.md#static-and-media-files)
- [Django: Managing static files](https://docs.djangoproject.com/en/stable/howto/static-files/)

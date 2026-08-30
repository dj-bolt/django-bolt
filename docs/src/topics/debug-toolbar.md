---
icon: lucide/bug
---

# Django Debug Toolbar

This guide shows how to use [Django Debug Toolbar](https://django-debug-toolbar.readthedocs.io/) with Django-Bolt.

The toolbar works on two kinds of routes:

- **Bolt routes** (`@api.get`, `@api.post`, ...) when you run the toolbar middleware on them with `BoltAPI(django_middleware=[...])`. HTML responses get the toolbar. JSON responses get the `djdt-request-id` and `Server-Timing` headers, and show up in the History panel.
- **Mounted Django views** (`api.mount_django(...)`). These run the full Django middleware stack, so the toolbar works as it does under `runserver`.

The SQL panel records queries from async Bolt handlers, including the async ORM.

## Install

```bash
pip install django-debug-toolbar
```

## Configure

Keep the toolbar behind `DEBUG`.

```python
# settings.py
DEBUG = True

INSTALLED_APPS = [
    "django_bolt",
    "django.contrib.staticfiles",
    # ...
]
if DEBUG:
    INSTALLED_APPS.append("debug_toolbar")

MIDDLEWARE = [
    *(["debug_toolbar.middleware.DebugToolbarMiddleware"] if DEBUG else []),
    "django.middleware.security.SecurityMiddleware",
    # ...
]

INTERNAL_IPS = ["127.0.0.1"]
```

The toolbar shows only when `request.META["REMOTE_ADDR"]` is in `INTERNAL_IPS`. Bolt sets `REMOTE_ADDR` on Bolt routes and on mounted Django views. Behind a reverse proxy, set `BOLT_TRUSTED_PROXIES` so `REMOTE_ADDR` is the real client address. In Docker, use `debug_toolbar.middleware.show_toolbar_with_docker` as `SHOW_TOOLBAR_CALLBACK`.

!!! note "Upgrading from Django-Bolt 0.10.3 or older"

    Older versions did not set `REMOTE_ADDR` on Bolt routes. The `INTERNAL_IPS` check never matched. If you set `SHOW_TOOLBAR_CALLBACK` to work around that, remove the override. The default check works now.

Add the toolbar URLs to your URLconf:

```python
# urls.py
from django.conf import settings
from django.urls import include, path

urlpatterns = [
    # ...
]

if "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))
```

Check `INSTALLED_APPS`, not `settings.DEBUG`. Test runners set `DEBUG = False` after the settings load. The middleware list is already built by then, and the toolbar middleware needs the `djdt` URL namespace.

## Enable the toolbar on Bolt routes

Run the toolbar middleware on Bolt routes, and mount the toolbar views at the site root:

```python
# api.py
from django.conf import settings
from django_bolt import BoltAPI

DEBUG_TOOLBAR_MIDDLEWARE = (
    ["debug_toolbar.middleware.DebugToolbarMiddleware"] if "debug_toolbar" in settings.INSTALLED_APPS else None
)

api = BoltAPI(django_middleware=DEBUG_TOOLBAR_MIDDLEWARE)

if "debug_toolbar" in settings.INSTALLED_APPS:
    api.mount_django("/__debug__", clear_root_path=True)
```

The toolbar on a Bolt route calls `/__debug__/render_panel/` and `/__debug__/history_sidebar/`. The mount serves these paths. `clear_root_path=True` makes Django see the full `/__debug__/...` path, which matches the URLconf entry.

A mounted Django view does not need this mount. The toolbar on a page under `api.mount_django("/django")` calls `/django/__debug__/...`, which the same mount serves.

Pass a list with only the toolbar middleware. `django_middleware=True` runs all of `settings.MIDDLEWARE` on Bolt routes, and `CsrfViewMiddleware` then rejects API `POST` requests with `403`. See [Middleware](middleware.md#django-middleware-integration) for the per-request cost.

## Static files

The toolbar ships its CSS and JS as app static files. In `DEBUG`, Bolt serves them through Django's staticfiles finders. No extra setup is needed. See [Static Files](static-files.md).

## Run

```bash
python manage.py runbolt --dev
```

`--dev` runs one process with auto-reload. The toolbar needs one process. The default `MemoryStore` keeps panel data in the worker that served the request. With more workers, a panel request can land on a different worker and return `404`. To run more workers without `--dev`, store panel data in the database:

```python
DEBUG_TOOLBAR_CONFIG = {
    "TOOLBAR_STORE_CLASS": "debug_toolbar.store.DatabaseStore",
}
```

Then run `python manage.py migrate`.

## See API requests

A JSON response cannot hold the toolbar HTML. The toolbar still records the request. To see it:

1. Open a page that shows the toolbar, for example `/dashboard`.
2. Call the API. Use the browser, `curl`, or your frontend.
3. Open the **History** panel in the toolbar. Click **Refresh**.
4. Find the API request in the list. Click **Switch**.
5. Open the **SQL** panel, or any other panel.

All panels now show that API request: SQL, Templates, Headers, Timer, and the others.

The **Request Variables** column in the History list holds only the GET and POST parameters of that request. `No data` there does not mean the request ran no queries. Use **Switch** and the SQL panel for the queries.

Each API response also has two headers you can read in the browser's network tab or with `curl -i`:

- `djdt-request-id` - the id of the record in the toolbar store.
- `Server-Timing` - CPU time, elapsed time, SQL time, and cache time.

## What to expect

| Request | Result |
|---|---|
| Bolt route that returns HTML (`render(...)`, `HTML(...)`) | Toolbar is in the page. |
| Bolt route that returns JSON | `djdt-request-id` and `Server-Timing` headers. Open any page with the toolbar and use the History panel. |
| Mounted Django HTML view | Toolbar is in the page. |
| Streaming or compressed response | No toolbar. The toolbar only edits a complete, uncompressed `text/html` body. |

## Example project

`python/example` has this setup. `DEBUG` is on by default there. Start it and open these pages:

```bash
cd python/example
python manage.py runbolt --dev --host 127.0.0.1 --port 8001
```

- `http://127.0.0.1:8001/dashboard` - a Bolt route that renders a template with the async ORM. The SQL panel shows the query.
- `http://127.0.0.1:8001/django/missions/` - a mounted Django view that renders the same template. The SQL and Templates panels show the query and the context.
- `http://127.0.0.1:8001/django/admin/login/` - the mounted Django admin.
- `http://127.0.0.1:8001/health` - a Bolt JSON route. Find it in the History panel.

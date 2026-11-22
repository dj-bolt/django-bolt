---
description: Show django-debug-toolbar on Bolt routes and mounted Django views, and inspect the SQL queries of JSON API requests.
---

# Django Debug Toolbar

[Django Debug Toolbar](https://django-debug-toolbar.readthedocs.io/) shows the SQL queries, the templates and the timing of a request.
It works on Bolt routes when you run its middleware on them.
It also works on Django views that you mount with `api.mount_django()`.

## Install the toolbar

```bash
pip install django-debug-toolbar
```

Add the toolbar to the settings. Keep it behind `DEBUG`:

```python title="blogsite/settings.py"
DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "django_bolt",
]
if DEBUG:
    INSTALLED_APPS.append("debug_toolbar")

MIDDLEWARE = [
    *(["debug_toolbar.middleware.DebugToolbarMiddleware"] if DEBUG else []),
    "django.middleware.common.CommonMiddleware",
]

INTERNAL_IPS = ["127.0.0.1"]
STATIC_URL = "static/"
```

Add the toolbar URLs to the URLconf:

```python title="blogsite/urls.py"
from django.conf import settings
from django.urls import include, path

urlpatterns = []

if "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))
```

## Show the toolbar on Bolt routes

Run the toolbar middleware on the Bolt routes with `django_middleware`.
Then mount Django at `/__debug__`:

```python title="blogsite/api.py"
from django.conf import settings
from django.contrib.auth.models import User

from django_bolt import BoltAPI
from django_bolt.responses import HTML

TOOLBAR_MIDDLEWARE = (
    ["debug_toolbar.middleware.DebugToolbarMiddleware"] if "debug_toolbar" in settings.INSTALLED_APPS else None
)

api = BoltAPI(django_middleware=TOOLBAR_MIDDLEWARE)


@api.get("/authors/count")
async def author_count() -> dict:
    return {"count": await User.objects.acount()}


@api.get("/dashboard")
async def dashboard() -> HTML:
    count = await User.objects.acount()
    return HTML(f"<html><body><h1>{count} authors</h1></body></html>")


if "debug_toolbar" in settings.INSTALLED_APPS:
    api.mount_django("/__debug__", clear_root_path=True)
```

Run the server:

```bash
python manage.py runbolt --dev
```

Open `http://127.0.0.1:8000/dashboard` in a browser. The toolbar shows on the right side of the page.

The toolbar loads its panels from URLs that start with `/__debug__/`.
Only Django can serve them, so the mount at `/__debug__` is necessary.
`clear_root_path=True` gives Django the full path, which `urls.py` matches.

Give `django_middleware` a list with only the toolbar.
`django_middleware=True` runs each entry of `MIDDLEWARE` on the Bolt routes.
`CsrfViewMiddleware` then refuses each API `POST` with `403`.
See [Middleware](middleware.md).

`request.user` and `await request.auser()` work in the handlers.
The toolbar is Django middleware, so Bolt runs each request on its own thread, and the user query runs there.

## See the queries of an API request

A JSON response cannot hold the toolbar.
The toolbar still records the request and adds two headers:

```bash
curl -i localhost:8000/authors/count
```

```text
HTTP/1.1 200 OK
content-length: 11
server-timing: TimerPanel_utime;dur=21.524999999999906;desc="User CPU time", TimerPanel_stime;dur=23.663999999999998;desc="System CPU time", TimerPanel_total;dur=45.18899999999991;desc="Total CPU time", TimerPanel_total_time;dur=45.24872299998606;desc="Elapsed time", TimerPanel_toolbar_time;dur=45.387284000071304;desc="Including DebugToolbar time", SQLPanel_sql_time;dur=0.08111900001495087;desc="SQL 1 queries", CachePanel_total_time;dur=0;desc="Cache 0 Calls"
djdt-request-id: 85c0b076b1ae45fcb6e12e251aac4025
content-type: application/json

{"count":0}
```

- `djdt-request-id` is the id of the record in the toolbar store.
- `Server-Timing` has the CPU time, the elapsed time, the SQL time and the cache time. The network tab of the browser shows it.

To see all the panels of an API request:

1. Open a page that shows the toolbar, for example `/dashboard`.
2. Send the API request, for example from your frontend or with `curl`.
3. Open the **History** panel and click **Refresh**.
4. Find the API request and click **Switch**.
5. Open the **SQL** panel or another panel.

The **Request Variables** column of the History panel shows only the GET and POST data.
`No data` in that column does not mean that the request made no queries.

## Show the toolbar on mounted Django views

A view that you serve with `api.mount_django()` runs the full `MIDDLEWARE` of Django.
The toolbar works there as under `runserver`.
A view under `api.mount_django("/pages")` loads its panels from `/pages/__debug__/`, and the same mount serves them.

## Show the toolbar on the API docs

The `django_middleware` of the API also runs on the API documentation routes.
Thus an HTML documentation page, for example Scalar at `/docs`, shows the toolbar.
The JSON schema at `/docs/openapi.json` cannot hold the toolbar.

A custom HTML render plugin must have a closing `</body>` tag. The toolbar puts its HTML before that tag.

## Serve the toolbar files

The toolbar has CSS and JavaScript files in its app static files.
With `DEBUG = True`, Bolt serves them through the staticfiles finders of Django.
See [Static files](static-files.md).

## Run more than one process

The default store of the toolbar, `MemoryStore`, keeps the panel data in the process that served the request.
With `runbolt --processes 2` or more, a panel request can go to another process.
The panel then shows `Data for this panel isn't available anymore`.

`runbolt --dev` runs one process. To run more processes, keep the panel data in the database:

```python title="blogsite/settings.py"
DEBUG_TOOLBAR_CONFIG = {
    "TOOLBAR_STORE_CLASS": "debug_toolbar.store.DatabaseStore",
}
```

Then run `python manage.py migrate`.

## Show the toolbar behind a proxy or in Docker

The toolbar shows only when `REMOTE_ADDR` is in `INTERNAL_IPS`.
Bolt sets `REMOTE_ADDR` to the client address.
Behind a reverse proxy, set `BOLT_TRUSTED_PROXIES`, so `REMOTE_ADDR` is the address of the real client.
In Docker, set `SHOW_TOOLBAR_CALLBACK` to `"debug_toolbar.middleware.show_toolbar_with_docker"` in `DEBUG_TOOLBAR_CONFIG`.

## Know when the toolbar does not show

| Response | Result |
| --- | --- |
| HTML from a Bolt route, for example `HTML(...)` or `render(...)` | The toolbar is in the page. |
| JSON from a Bolt route | The `djdt-request-id` and `Server-Timing` headers. Use the History panel. |
| HTML from a mounted Django view | The toolbar is in the page. |
| A streaming response | No toolbar. The toolbar changes only a complete `text/html` body. |

The example project in `python/example` has this setup. Set `DEBUG = True` in `testproject/settings.py` to turn it on.

## See also

- [Middleware](middleware.md)
- [ASGI mounts](asgi-mounts.md)
- [Static files](static-files.md)
- [Settings reference](../ref/settings.md)

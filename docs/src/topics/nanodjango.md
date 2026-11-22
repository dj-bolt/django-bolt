---
description: Write a Django project with Bolt routes in one file with nanodjango and the django_bolt.nanodjango plugin.
---

# nanodjango

[nanodjango](https://github.com/radiac/nanodjango) puts a Django project in one Python file.
The `django_bolt.nanodjango` plugin adds Bolt routes to such a file and configures the settings for `runbolt`.
Use it for a prototype, a small service or a bug report.

Install Bolt with the `nanodjango` extra:

```bash
pip install "django-bolt[nanodjango]"
```

Write the app in one file:

```python title="blog.py"
# /// script
# dependencies = ["django-bolt[nanodjango]"]
# ///
import sys

import msgspec
from nanodjango import Django

from django_bolt.nanodjango import BoltAPI

app = Django()
api = BoltAPI()


class Article(msgspec.Struct):
    id: int
    title: str


@app.route("/")
def home(request):
    return "<h1>Blog home</h1><p>This page is a Django view.</p>"


@api.get("/api/articles/{article_id}")
async def get_article(article_id: int) -> Article:
    return Article(id=article_id, title=f"Article {article_id}")


api.mount_django("/")

if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
```

```bash
python blog.py runbolt
```

```bash
curl localhost:8000/api/articles/3
```

```json
{"id":3,"title":"Article 3"}
```

```bash
curl localhost:8000/
```

```html
<h1>Blog home</h1><p>This page is a Django view.</p>
```

`/docs` shows the OpenAPI documentation of the Bolt routes, as in a full project.

## Run the app

Use one of these commands:

| Command | Use it when |
| --- | --- |
| `python blog.py runbolt` | The file has the `if __name__ == "__main__":` block. |
| `nanodjango manage blog.py runbolt` | You use the nanodjango CLI. The file does not need the `__main__` block. |
| `uv run blog.py runbolt` | You want uv to install the dependencies of the `# /// script` block in a temporary environment. |

Each `runbolt` option works, for example `--port 8001`, `--dev` or `--processes 4`.
See the [runbolt reference](../ref/runbolt.md).

Give the file a name that Python can import, for example `blog.py`, not `my-blog.py`.

## Know what the plugin does

`django_bolt.nanodjango.BoltAPI` is a subclass of `django_bolt.BoltAPI`. It adds these steps:

- It adds `django_bolt` to `INSTALLED_APPS`.
- At the first route, it finds the name of the variable that holds the API, for example `api`. It then sets `BOLT_API`, so `runbolt` finds the API.

nanodjango loads the plugin through an entry point. You do not configure it.

Create the `BoltAPI` after `app = Django()`, because `Django()` configures the settings.

## Serve Django views on the same port

`runbolt` serves the Bolt routes.
A view of `@app.route()` is a Django view, so `runbolt` serves it only through a mount.
`api.mount_django("/")` sends each path that has no Bolt route to Django.
Without the mount, `curl localhost:8000/` gets `404`.

Bolt routes always come before the mount, also when the mount is above them in the file.
To send only some paths to Django, mount a prefix, for example `api.mount_django("/pages")`.
See [ASGI mounts](asgi-mounts.md).

## Create the API in another module

The plugin reads the module that calls `BoltAPI()`.
When a helper in another module creates the API, give the module of the variable with `module=`:

```python title="bolt_setup.py"
from django_bolt.nanodjango import BoltAPI


def make_api(module: str) -> BoltAPI:
    return BoltAPI(module=module, prefix="/api")
```

```python title="blog.py"
import sys

from nanodjango import Django

app = Django()

from bolt_setup import make_api  # noqa: E402

api = make_api(__name__)


@api.get("/ping")
async def ping() -> dict:
    return {"ping": "pong"}


if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
```

The plugin looks for the variable at the first route.
Thus register the routes after the assignment, at the top level of the module.
A route in the function that creates the API comes before the assignment.
The plugin then cannot find the API, and it logs a warning on the `django_bolt.nanodjango` logger.

## Convert the app to a full project

When the app grows, convert it to a Django project:

```bash
nanodjango convert blog.py blogsite --name=blogsite
```

The plugin moves each statement that adds a route to `api.py` of the new app:

- `BoltAPI()` and `Router()` assignments.
- Functions with a route decorator, for example `@api.get(...)`.
- Classes with `@api.view(...)` or `@api.viewset(...)`.
- Calls to `mount()`, `mount_django()`, `mount_asgi()`, `mount_mcp()`, `include_router()` and `native_route()`.

The plugin also adds `django_bolt` to `INSTALLED_APPS` of the new `settings.py`.

Then edit the new `api.py`:

- Remove the `from ninja import NinjaAPI` line and the `api = NinjaAPI()` line. nanodjango adds them.
- Change `from django_bolt.nanodjango import BoltAPI` to `from django_bolt import BoltAPI`.

`runbolt` then finds `api.py` in the app. See [Routing](routing.md#find-the-api-modules).

## See also

- [Installation](../intro/install.md)
- [ASGI mounts](asgi-mounts.md)
- [runbolt reference](../ref/runbolt.md)
- [nanodjango documentation](https://nanodjango.readthedocs.io/)

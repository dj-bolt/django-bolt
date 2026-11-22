---
description: Install Django-Bolt, add it to a Django project, write an api.py file and start the server with runbolt.
---

# Installation

## Requirements

| Item | Supported |
| --- | --- |
| Python | CPython 3.12, 3.13 and 3.14. The free-threaded build of 3.14 (`3.14t`) also has a wheel. |
| Django | 5.2, 6.0 and 6.1. The lowest version is Django 5.2.19. |

Django-Bolt has a prebuilt wheel for these platforms:

| Operating system | CPU |
| --- | --- |
| Linux (glibc, manylinux) | x86_64 and ARM64 (aarch64) |
| macOS | Apple silicon (arm64) and Intel (x86_64) |
| Windows | x64 |

One wheel serves CPython 3.12 and later on each platform. A separate wheel serves the free-threaded build of 3.14.

On a platform with no wheel, for example Alpine Linux (musl) or PyPy, pip builds the package from the source distribution.
The build needs the Rust toolchain. Install it with [rustup](https://rustup.rs/) before you install Django-Bolt.

## Install the package

Install with pip:

```bash
python -m pip install django-bolt
```

Or add it to a [uv](https://docs.astral.sh/uv/) project:

```bash
uv add django-bolt
```

The package installs Django, msgspec, PyJWT and the other dependencies that it needs.
On Linux and macOS, it also installs uvloop.

Check the installed version:

```bash
django-bolt version
```

```text
Django-Bolt version: 0.13.0
```

## Add Django-Bolt to a project

Add `django_bolt` to `INSTALLED_APPS`:

```python title="mysite/settings.py"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_bolt",
]
```

This adds the `runbolt` management command.

## Create an `api.py` file

Create `api.py` in the project package, next to `settings.py` and `urls.py`:

```python title="mysite/api.py"
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/hello")
async def hello() -> dict:
    return {"message": "Hello from Django-Bolt"}
```

## How `runbolt` finds your APIs

`runbolt` looks for a `BoltAPI` instance in these modules, in this order:

1. `api.py`, then `bolt_api.py`, in the package of `ROOT_URLCONF`. For `ROOT_URLCONF = "mysite.urls"`, this is `mysite/api.py`.
2. `api.py`, then `bolt_api.py`, in each app in `INSTALLED_APPS`.

In each module, `runbolt` takes the first top-level assignment of `BoltAPI()`, for example `api = BoltAPI()`.
It reads the source of the module to find the assignment. It imports only the modules that have one.

`runbolt` merges all the APIs that it finds into one server.
If two APIs have the same method and path, `runbolt` stops with an error:

```text
CommandError: Route conflict: GET /hello defined in both mysite.api:api and articles.api:api
```

To choose the APIs yourself, set `BOLT_API` to a list of `"module:attribute"` paths.
Then `runbolt` loads only these APIs and does no search:

```python title="mysite/settings.py"
BOLT_API = ["mysite.api:api"]
```

## Start the server

Apply the migrations of Django, then start the server in development mode:

```bash
python manage.py migrate
python manage.py runbolt --dev
```

```text
System check identified no issues (0 silenced).

  ⚡ Django Bolt v0.13.0  ready in 446 ms

  ➜  Local: http://127.0.0.1:8000
  ➜  Docs:  http://127.0.0.1:8000/docs
  ➜  Admin: http://127.0.0.1:8000/admin/

  Mode           development · auto-reload
  Routes         1 API · 1 ASGI · +8 framework · 1 app
  Workers        1 process
  Compression    enabled
  Middleware     9 handlers

  press CTRL+C to stop
```

`--dev` restarts the server when you change a Python file.
The server listens on `0.0.0.0:8000`. Use `--host` and `--port` to change this.
For all the options, see [runbolt](../ref/runbolt.md).

## Check that it works

Send a request from a second terminal:

```bash
curl localhost:8000/hello
```

```json
{"message":"Hello from Django-Bolt"}
```

Open `http://127.0.0.1:8000/docs` in a browser. It shows the Swagger UI page for your API.

## Optional extras

| Extra | Install command | Adds |
| --- | --- | --- |
| `mcp` | `pip install "django-bolt[mcp]"` | The `bolt-mcp` package, for MCP servers. See [MCP](../topics/mcp.md). |
| `nanodjango` | `pip install "django-bolt[nanodjango]"` | nanodjango, for single-file projects. See [nanodjango](../topics/nanodjango.md). |
| `pool` | `pip install "django-bolt[pool]"` | psycopg with `psycopg-pool` 3.3 or later, for the connection pool of Django on PostgreSQL. |

## Troubleshooting

### `Unknown command: 'runbolt'`

`django_bolt` is not in `INSTALLED_APPS`. Add it, as in [Add Django-Bolt to a project](#add-django-bolt-to-a-project).

### `No BoltAPI instances found`

```text
CommandError: No BoltAPI instances found. Create api.py files with a top-level BoltAPI() assignment, e.g., api = BoltAPI()
```

`runbolt` found no `BoltAPI()` assignment. Do these checks:

- The file has the name `api.py` or `bolt_api.py`.
- The file is in the project package or in an app in `INSTALLED_APPS`.
- The assignment is at the top level of the module, not in a function or an `if` block.

### `Address already in use`

The traceback ends with this line:

```text
RuntimeError: Server error: Address already in use (os error 98)
```

A different process uses the port. Stop that process, or start Bolt on a different port:

```bash
python manage.py runbolt --dev --port 8001
```

### pip builds Django-Bolt from source and fails

The error contains this text:

```text
Cargo, the Rust package manager, is not installed or is not on PATH.
```

Your platform or your Python build has no wheel. Do one of these steps:

- Use a supported Python version and platform from [Requirements](#requirements).
- Install Rust with [rustup](https://rustup.rs/), then install Django-Bolt again.

### `--processes` fails on Windows

```text
CommandError: --processes > 1 and worker recycling require os.fork(), which is unavailable on this platform
```

Windows has no `os.fork()`. On Windows, run one process, or deploy on Linux.

### A worker stops on macOS with PostgreSQL

A forked worker can hang when libpq uses GSSAPI on macOS. See the [FAQ](../faq.md).

## See also

- [Tutorial part 1](../tutorial/index.md): start a new project with Django-Bolt.
- [runbolt reference](../ref/runbolt.md): all the server options.
- [Settings reference](../ref/settings.md): the `BOLT_*` settings.
- [Deployment](../topics/deployment.md): run Bolt in production.

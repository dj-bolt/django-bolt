---
description: Tutorial part 1. Create a Django project, install Django-Bolt, add a first route, start runbolt and open the API docs.
---

# Tutorial part 1: project and first route

In this tutorial, you build the API of a small publishing site.
Authors write articles. Readers list and read them.

The tutorial has four parts:

1. Create the project and a first route. (This page.)
2. [Add an `Article` model and the routes to list, read, create, change and delete articles.](part-2.md)
3. [Add JWT authentication. Only the author of an article can change it.](part-3.md)
4. [Write tests, and run the server with several processes.](part-4.md)

You must know Python and the basics of Django. If Django is new to you, do the [Django tutorial](https://docs.djangoproject.com/en/stable/intro/tutorial01/) first.
You need Python 3.12 or later. For the full list of requirements, see [Installation](../intro/install.md).

## Create a virtual environment

Make a directory for the project, and create a virtual environment in it:

```bash
mkdir newsroom
cd newsroom
python -m venv .venv
source .venv/bin/activate
```

On Windows, activate the environment with `.venv\Scripts\activate`.

## Install Django-Bolt

```bash
python -m pip install django-bolt
```

This also installs Django. Check the version of Django-Bolt:

```bash
django-bolt version
```

```text
Django-Bolt version: 0.13.0
```

## Create the Django project

Create a project with the name `mysite` in the current directory:

```bash
django-admin startproject mysite .
```

The directory now has this structure:

```text
newsroom/
    manage.py
    mysite/
        __init__.py
        asgi.py
        settings.py
        urls.py
        wsgi.py
```

## Add Django-Bolt to the project

Add `"django_bolt"` to the end of `INSTALLED_APPS`:

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

Create the database tables of Django:

```bash
python manage.py migrate
```

```text
Operations to perform:
  Apply all migrations: admin, auth, contenttypes, sessions
Running migrations:
  Applying contenttypes.0001_initial... OK
  Applying auth.0001_initial... OK
  ...
  Applying sessions.0001_initial... OK
```

## Add your first route

Create the file `mysite/api.py`:

```python title="mysite/api.py"
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/hello")
async def hello() -> dict:
    return {"message": "Hello from Django-Bolt"}
```

`BoltAPI` holds the routes of your API.
`@api.get("/hello")` connects the route `GET /hello` to the handler `hello`.
The handler returns a `dict`, and Bolt sends it as JSON.

You do not add this file to `urls.py`.
`runbolt` finds an `api.py` file in the project package and in each installed app.
For the rules, see [How runbolt finds your APIs](../intro/install.md#how-runbolt-finds-your-apis).

## Start the server

```bash
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

`runbolt` is the HTTP server of Django-Bolt. It replaces `runserver` in development, and gunicorn or uvicorn in production.
`--dev` restarts the server when you change a Python file. Keep it running for the rest of the tutorial.

The banner shows:

- `1 API`: your route `GET /hello`.
- `1 ASGI`: the Django admin, at `/admin/`.
- `+8 framework`: the routes that Bolt adds for the API docs at `/docs`.

## Send a request

Open a second terminal and send a request:

```bash
curl localhost:8000/hello
```

```json
{"message":"Hello from Django-Bolt"}
```

The terminal of the server logs the request:

```text
[22/Nov/2026 01:58:43,814] GET /hello 200 0.1ms
```

## Open the API docs

Open `http://127.0.0.1:8000/docs` in a browser.
Bolt makes an OpenAPI schema from your routes, and shows it in Swagger UI.
Click `GET /hello`, then **Try it out**, then **Execute**. The page sends the request and shows the response.

The schema is at `http://127.0.0.1:8000/docs/openapi.json`.
To change the title or the UI, see [OpenAPI](../topics/openapi.md).

## Summary

You made a Django project that serves an API with `runbolt`.
You did not change `urls.py`, and you did not install an app server.

**Next:** [Part 2: models, validation and the ORM](part-2.md)

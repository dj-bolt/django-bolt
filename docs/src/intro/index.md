---
description: Django-Bolt is a typed API framework and a Rust HTTP server for Django projects. Learn what it does, what you keep, and when not to use it.
---

# Introduction

Django-Bolt is an API framework and an HTTP server for Django.
You write typed handlers in Python, in a normal Django project.
A Rust HTTP server ([Actix Web](https://actix.rs/)) receives each request and calls your handler.

You start the server with `python manage.py runbolt`.
The same command serves your API in development and in production.
You do not need gunicorn or uvicorn.

## A first look

```python title="mysite/api.py"
import msgspec
from django.contrib.auth.models import User

from django_bolt import BoltAPI

api = BoltAPI()


class UserOut(msgspec.Struct):
    id: int
    username: str


@api.get("/users")
async def list_users(is_staff: bool = False) -> list[UserOut]:
    return User.objects.filter(is_staff=is_staff)
```

```bash
curl "localhost:8000/users?is_staff=true"
```

```json
[{"id":1,"username":"alice"}]
```

The type hint `is_staff: bool` makes a query parameter. Bolt converts it and rejects a value that is not a boolean.
The return type `list[UserOut]` tells Bolt how to serialize the QuerySet.
Bolt runs the query in a thread pool, so the query does not block other requests.

## What you keep from Django

Django-Bolt replaces the HTTP server and the API layer. The rest of your project stays the same:

- Models, migrations and the ORM, with the sync and the async query API.
- `settings.py`, `INSTALLED_APPS` and the `manage.py` commands.
- The Django admin. `runbolt` serves it at the path of your URLconf, for example `/admin/`.
- Users, passwords and permissions from `django.contrib.auth`.
- Signals, and Django middleware on the APIs where you turn it on. See [Middleware](../topics/middleware.md).
- Your other Django views. Serve them with `api.mount_django()`. See [ASGI mounts](../topics/asgi-mounts.md).

## What Django-Bolt adds

- Parameters from type hints: path, query, header, cookie, form, file and JSON body. See [Requests](../topics/requests.md).
- Validation and serialization with [msgspec](https://jcristharif.com/msgspec/). See [Serializers](../topics/serializers.md).
- JWT, API key and session authentication, and guards. Bolt checks tokens and guards in Rust before it calls Python. See [Authentication](../topics/authentication.md).
- OpenAPI docs at `/docs`. See [OpenAPI](../topics/openapi.md).
- CORS, rate limiting and compression, configured in Python and applied in Rust.
- Pagination, class-based views and viewsets.
- WebSocket, server-sent events and streaming responses.
- A test client that sends each request through the same Rust pipeline as the server. See [Testing](../topics/testing.md).
- A server with several processes on one port: `runbolt --processes 4`. See [Deployment](../topics/deployment.md).

## When to use Django-Bolt

- You have a Django project and you need a JSON API.
- You want typed handlers, as in FastAPI or Django Ninja, with the Django ORM and admin.
- You want more requests per second from each CPU. The [Benchmarks](../benchmarks.md) page gives the numbers and the conditions.

## When not to use Django-Bolt

- **You need a stable API.** Django-Bolt is at version 0.x. A minor release can change an API. Read the [changelog](https://github.com/dj-bolt/django-bolt/blob/master/CHANGELOG.md) before you upgrade.
- **Your platform needs a WSGI or an ASGI application.** Bolt routes run only in `runbolt`. A serverless platform or a host that starts only gunicorn or uvicorn cannot serve them.
- **You depend on Django REST Framework.** DRF serializers, permission classes and DRF packages do not apply to Bolt routes. Bolt has its own serializers, viewsets and guards. See [Migrate from Django Ninja, DRF or FastAPI](../topics/migrating.md).
- **You use Pydantic models.** Bolt validates with msgspec. A Pydantic model is not a valid request body type.
- **You deploy on Windows with several processes.** `runbolt --processes` with a value above 1 needs `os.fork()`, and Windows does not have it.

## Next steps

- [Installation](install.md): requirements and setup in an existing project.
- [Tutorial](../tutorial/index.md): build a publishing API in four parts.
- [How it works](../architecture.md): the request path from Rust to Python.
- [FAQ](../faq.md): short answers to common questions.

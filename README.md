<div align="center">
  <img src="https://raw.githubusercontent.com/dj-bolt/django-bolt/master/docs/logo.png" alt="Django-Bolt" width="360"/>

  <h3>Django APIs on a Rust server</h3>

  <p>
    <a href="https://pypi.org/project/django-bolt/"><img src="https://img.shields.io/pypi/v/django-bolt.svg?color=blue" alt="PyPI"/></a>
    <a href="https://pypi.org/project/django-bolt/"><img src="https://img.shields.io/pypi/pyversions/django-bolt.svg" alt="Python versions"/></a>
    <a href="https://pypi.org/project/django-bolt/"><img src="https://img.shields.io/badge/Django-5.2%20%7C%206.0%20%7C%206.1-0C4B33?logo=django&logoColor=white" alt="Django versions"/></a>
    <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"/></a>
    <a href="https://pepy.tech/projects/django-bolt"><img src="https://static.pepy.tech/personalized-badge/django-bolt?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads" alt="Downloads"/></a>
    <a href="https://discord.gg/4xErptXK82"><img src="https://img.shields.io/discord/1513537500000292894?logo=discord&logoColor=white&label=Discord&color=5865F2" alt="Discord"/></a>
  </p>

  <p>
    <a href="https://bolt.farhana.li/"><b>Documentation</b></a> ·
    <a href="https://bolt.farhana.li/tutorial/"><b>Tutorial</b></a> ·
    <a href="https://bolt.farhana.li/benchmarks/"><b>Benchmarks</b></a> ·
    <a href="https://bolt.farhana.li/comparison/"><b>Comparison</b></a> ·
    <a href="https://discord.gg/4xErptXK82"><b>Discord</b></a>
  </p>
</div>

Django-Bolt is an API framework and an HTTP server for Django.
You write typed handlers in Python, in a normal Django project.
A Rust server ([Actix Web](https://actix.rs/)) receives each request, and runs routing, authentication, CORS and rate limits before it calls your handler.
Your models, admin, migrations and settings do not change.

```python
import msgspec
from django.contrib.auth.models import User

from django_bolt import BoltAPI

api = BoltAPI()


class UserOut(msgspec.Struct):
    id: int
    username: str


@api.get("/users")
async def list_users(limit: int = 20) -> list[UserOut]:
    return User.objects.order_by("id")[:limit]
```

```bash
python manage.py runbolt --dev
curl "localhost:8000/users?limit=2"
# [{"id":1,"username":"alice"},{"id":2,"username":"bob"}]
```

`runbolt` is also the production server. You do not need gunicorn or uvicorn.

## Speed

| Test | Result |
| --- | ---: |
| JSON route, 8 processes, 100 connections | 324,493 req/s |
| 1 KB JSON, 1 process, against Litestar | 2.8× |
| 1 KB JSON, 1 process, against FastAPI | 5.8× |
| 10 rows from PostgreSQL, 1 process, against Litestar and FastAPI | 2.2× and 2.3× |

Measured on an AMD Ryzen 5 5600G with bombardier. The [Benchmarks](https://bolt.farhana.li/benchmarks/) page gives all results, their conditions and the commands to reproduce them.

## Install

Django-Bolt needs Python 3.12 or later and Django 5.2 or later. See [Installation](https://bolt.farhana.li/intro/install/) for the full requirements.

```bash
pip install django-bolt
```

Add the app to your settings:

```python
INSTALLED_APPS = [
    # ...
    "django_bolt",
]
```

Create `api.py` next to your `urls.py`, or in any installed app. `runbolt` finds it when it starts:

```python
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/hello/{name}")
async def hello(name: str) -> dict:
    return {"message": f"Hello, {name}"}
```

Start the server:

```bash
python manage.py runbolt --dev            # development, reloads on change
python manage.py runbolt --processes 4    # production
```

Open `http://localhost:8000/docs` for the OpenAPI docs.
Then follow the [tutorial](https://bolt.farhana.li/tutorial/). It builds an articles API in four parts.

## Examples

### Validate a request

Bolt reads each argument from its type hint: path, query, header, cookie, form, file or JSON body.
A bad value gets a 422 response, and the handler does not run.

```python
from typing import Annotated

import msgspec

from django_bolt import BoltAPI
from django_bolt.params import Header

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: str
    body: str


@api.post("/articles", status_code=201)
async def create_article(
    article: ArticleIn,
    client: Annotated[str, Header(alias="x-client")],
    notify: bool = False,
) -> dict:
    return {"title": article.title, "client": client, "notify": notify}
```

### Protect a route

Rust checks the token and the guards before Python runs.

```python
from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

api = BoltAPI()


@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def me(request) -> dict:
    user = await request.auser()
    return {"username": user.username}
```

### Serve a model with a viewset

```python
from django.contrib.auth.models import User

from django_bolt import BoltAPI, ModelViewSet, PageNumberPagination
from django_bolt.serializers import Serializer

api = BoltAPI()


class UserSerializer(Serializer):
    id: int
    username: str
    email: str


@api.viewset("/users")
class UserViewSet(ModelViewSet):
    queryset = User.objects.order_by("id")
    serializer_class = UserSerializer
    pagination_class = PageNumberPagination
```

### Stream events and use WebSocket

```python
import asyncio
from collections.abc import AsyncIterator

from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse
from django_bolt.websocket import WebSocket

api = BoltAPI()


@api.get("/ticks", response_class=EventSourceResponse)
async def ticks() -> AsyncIterator[dict]:
    for tick in range(3):
        yield {"tick": tick}
        await asyncio.sleep(1)


@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(message)
```

### Limit and allow callers

```python
from django_bolt import BoltAPI
from django_bolt.middleware import cors, rate_limit

api = BoltAPI()


@api.get("/public")
@cors(origins=["https://example.com"])
@rate_limit(rps=100, burst=200)
async def public() -> dict:
    return {"ok": True}
```

### Serve tools to LLM clients

Install the MCP add-on with `pip install "django-bolt[mcp]"`.

```python
from bolt_mcp import MCP

from django_bolt import BoltAPI

api = BoltAPI()
mcp = MCP("articles")


@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


api.mount_mcp(mcp)
```

## What you get

| Area | Features |
| --- | --- |
| [Requests](https://bolt.farhana.li/topics/requests/) | Typed path, query, header, cookie, form, file and body parameters |
| [Responses](https://bolt.farhana.li/topics/responses/) | JSON, HTML, redirects, files, streaming, [server-sent events](https://bolt.farhana.li/topics/sse/) |
| [Serializers](https://bolt.farhana.li/topics/serializers/) | msgspec structs, field sets, computed fields, model integration |
| [Django ORM](https://bolt.farhana.li/topics/async-orm/) | Return a QuerySet from an async handler, or use the async ORM |
| [Authentication](https://bolt.farhana.li/topics/authentication/) | JWT, API key and session auth, checked in Rust |
| [Permissions](https://bolt.farhana.li/topics/permissions/) | `IsAuthenticated`, `AllowAny`, and claim checks with `Requires(...)` |
| [CORS](https://bolt.farhana.li/topics/cors/), [rate limits](https://bolt.farhana.li/topics/rate-limiting/), [compression](https://bolt.farhana.li/topics/compression/) | Applied in Rust |
| [Middleware](https://bolt.farhana.li/topics/middleware/) | Bolt middleware, and Django middleware when you turn it on |
| [Class-based views](https://bolt.farhana.li/topics/class-based-views/) | `APIView`, `ViewSet`, `ModelViewSet`, `@action` |
| [Pagination](https://bolt.farhana.li/topics/pagination/) | Page number, limit and offset, cursor |
| [Dependencies](https://bolt.farhana.li/topics/dependencies/) | `Depends()` |
| [WebSocket](https://bolt.farhana.li/topics/websocket/) | Handlers with the same auth, guards and CORS as HTTP routes |
| [OpenAPI](https://bolt.farhana.li/topics/openapi/) | Swagger UI, ReDoc, Scalar, RapiDoc and Stoplight Elements at `/docs` |
| [Static and media files](https://bolt.farhana.li/topics/static-files/) | Served by Rust, no WhiteNoise |
| [Testing](https://bolt.farhana.li/topics/testing/) | A test client that runs the same Rust pipeline as the server |
| [Deployment](https://bolt.farhana.li/topics/deployment/) | Many processes on one port, worker recycling, graceful stop |
| [MCP servers](https://bolt.farhana.li/topics/mcp/) | Tools, resources and prompts over MCP Streamable HTTP |

## When not to use Django-Bolt

- **You need a stable API.** Django-Bolt is at version 0.x. A minor release can change an API. Read the [changelog](CHANGELOG.md) before you upgrade.
- **Your host needs a WSGI or ASGI application.** Bolt routes run only in `runbolt`.
- **You depend on Django REST Framework packages or on Pydantic.** Bolt has its own serializers and validates with msgspec.

The [comparison](https://bolt.farhana.li/comparison/) gives the full list of trade-offs next to Django Ninja, DRF, FastAPI and Litestar.

## Contribute

[CONTRIBUTING.md](CONTRIBUTING.md) explains the setup (Rust toolchain, `uv`, `just`), the tests and the pull request rules.

```bash
git clone https://github.com/dj-bolt/django-bolt.git
cd django-bolt
uv sync
just build
just test-py
```

## Get help

- [Documentation](https://bolt.farhana.li/) and [FAQ](https://bolt.farhana.li/faq/)
- [Discord](https://discord.gg/4xErptXK82)
- [GitHub Issues](https://github.com/dj-bolt/django-bolt/issues)
- [Video walkthrough by BugBytes](https://www.youtube.com/watch?v=Pukr-fT4MFY)
- For AI assistants: [llms.txt](https://bolt.farhana.li/llms.txt)

## Sponsors

[Become a sponsor](https://opencollective.com/django-bolt) to support the development of Django-Bolt. Your logo will show here, with a link to your website.

<a href="https://opencollective.com/django-bolt/tiers/sponsor/0/website" target="_blank"><img src="https://opencollective.com/django-bolt/tiers/sponsor/0/avatar.svg" /></a>

### Backers

<a href="https://opencollective.com/django-bolt#backers" target="_blank"><img src="https://opencollective.com/django-bolt/backers.svg?width=890" /></a>

## Acknowledgments

Django-Bolt uses ideas and code from these projects:

- [Django REST Framework](https://github.com/encode/django-rest-framework): viewsets and permissions
- [FastAPI](https://github.com/fastapi/fastapi): dependency injection and parameters from type hints
- [Litestar](https://github.com/litestar-org/litestar): the OpenAPI plugins, middleware and guards
- [Robyn](https://github.com/sparckles/Robyn): an early Rust server for Python with PyO3
- [Actix Web](https://github.com/actix/actix-web), [PyO3](https://github.com/PyO3/pyo3), [msgspec](https://github.com/jcrist/msgspec) and [matchit](https://github.com/ibraheemdev/matchit): the foundations

## License

[MIT](https://opensource.org/licenses/MIT).

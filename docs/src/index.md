---
icon: lucide/zap
---

# Django-Bolt

Django-Bolt is a high-performance API framework for Django. It lets you build APIs using familiar Django patterns while leveraging Rust for speed.

## Installation

Install using pip:

```bash
pip install django-bolt
```

Or with uv:

```bash
uv add django-bolt
```

## At a glance

Here's a simple API endpoint:

```python
from django_bolt import BoltAPI

api = BoltAPI()

@api.get("/")
async def hello():
    return {"message": "Hello, World!"}

```

Add `django_bolt` to your `INSTALLED_APPS` in `settings.py`:

```python
INSTALLED_APPS = [
    ...
    "django_bolt",
]
```

Run it with:

```bash
python manage.py runbolt --dev
```

That's it. You now have an API endpoint at `http://localhost:8000`.

You also get automatic API documentation at `http://localhost:8000/docs`:

![OpenAPI Swagger UI](images/openapi-intro.png)

Other documentation UIs are also available by default: [Redoc](topics/openapi.md#redoc-only) (`/docs/redoc`), [Scalar](topics/openapi.md#scalar-only) (`/docs/scalar`), [RapiDoc](topics/openapi.md#rapidoc-only) (`/docs/rapidoc`), and [Stoplight Elements](topics/openapi.md#stoplight-elements-only) (`/docs/stoplight`). See the [OpenAPI documentation](topics/openapi.md) for customization options.

## Why Django-Bolt?

Django-Bolt is designed for developers who:

- Already know Django and want blazingly fast APIs
- Want type-safe request handling with automatic validation
- Prefer async/await for I/O-bound operations
- Need incremental migration from existing Django REST APIs—all Django features (ORM, authentication, middleware, signals, admin) work out of the box

## Key features

**Simple routing** - Decorator-based routing similar to FastAPI, Litestar and Flask:

```python
@api.get("/users/{user_id}")
async def get_user(user_id: int):
    return {"user_id": user_id}
```

**Automatic validation** - Request data is validated using Python type hints:

```python
import msgspec

class CreateUser(msgspec.Struct):
    username: str
    email: str

@api.post("/users")
async def create_user(user: CreateUser):
    return {"username": user.username}
```

**Django integration** - Works with your existing Django models and ORM:

```python
from myapp.models import User

@api.get("/users")
async def list_users():
    users = await User.objects.all().acount()
    return {"count": users}
```

**Built-in authentication** - JWT and API key authentication out of the box:

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated

@api.get("/profile", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def profile(request):
    return {"user_id": request.user.id}
```

**MCP servers** - Expose tools, resources, and prompts to LLM clients over the MCP Streamable HTTP transport via the optional [`bolt-mcp`](topics/mcp.md) package:

```python
from bolt_mcp import MCP

mcp = MCP("my-server")

@mcp.tool
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}

api.mount_mcp(mcp)
```

## Next steps

- **[Quick Start](getting-started/quickstart.md)** - Build your first API
- **[Deployment](getting-started/deployment.md)** - Deploy with multiple processes

## Getting help

- Check the [topic guides](topics/routing.md) for in-depth explanations
- Look at the [API reference](ref/api.md) for detailed information
- Report issues on [GitHub](https://github.com/FarhanAliRaza/django-bolt/issues)

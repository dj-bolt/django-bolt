---
icon: lucide/file-code-2
---

# OpenAPI Documentation

Django-Bolt automatically generates OpenAPI documentation for your API. This guide covers how to configure and customize the documentation.

## Accessing the documentation

By default, Django-Bolt serves multiple documentation UIs automatically:

| Path | UI |
|------|-----|
| `/docs` | Swagger UI (default) |
| `/docs/redoc` | Redoc |
| `/docs/scalar` | Scalar |
| `/docs/rapidoc` | RapiDoc |
| `/docs/stoplight` | Stoplight Elements |
| `/docs/openapi.json` | Raw JSON schema |
| `/docs/openapi.yaml` | Raw YAML schema |

Start your server and visit any of these URLs to browse your API documentation.

## Configuring OpenAPI

Customize the documentation using `OpenAPIConfig`:

```python
from django_bolt import BoltAPI
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        description="API for my application",
        enabled=True,
    )
)
```

## Available options

```python
OpenAPIConfig(
    title="My API",              # API title
    version="1.0.0",             # API version
    description="Description",   # API description
    enabled=True,                # Enable/disable docs
    docs_url="/docs",            # Swagger UI URL
    openapi_url="/openapi.json", # OpenAPI JSON URL
    django_auth=False,           # Enable Django admin auth for docs
)
```

## Documenting endpoints

### Summary and description

```python
@api.get(
    "/users/{user_id}",
    summary="Get a user",
    description="Retrieve a user by their unique ID.",
    tags=["users"]
)
async def get_user(user_id: int):
    """
    This docstring also appears in the documentation.

    Additional details about the endpoint can go here.
    """
    return {"user_id": user_id}
```

### Tags

Group endpoints using tags:

```python
@api.get("/users", tags=["users"])
async def list_users():
    return []

@api.post("/users", tags=["users"])
async def create_user():
    return {}

@api.get("/articles", tags=["articles"])
async def list_articles():
    return []
```

Tags appear as sections in the Swagger UI.

### Response models

Document response schemas:

```python
import msgspec

class User(msgspec.Struct):
    id: int
    username: str
    email: str

@api.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int):
    return {"id": user_id, "username": "john", "email": "john@example.com"}
```

The schema is automatically generated from the `msgspec.Struct`.

### Per-status-code response schemas

When `response_model` is a dict mapping status codes to types, the OpenAPI schema generates a separate response entry for each status code:

```python
@api.get("/items/{item_id}", response_model={200: Item, 404: Error})
async def get_item(item_id: int):
    ...
```

This produces OpenAPI responses with both `200` and `404` entries, each with their own schema.

Using `...` (ellipsis) as a key emits a `"default"` response entry in the OpenAPI spec, which represents any status code not explicitly listed:

```python
@api.get("/items/{item_id}", response_model={200: Item, ...: Error})
async def get_item(item_id: int):
    ...
```

Using `{204: None}` generates a response entry with no content body, appropriate for No Content responses.

### Request body schemas

Request bodies are documented automatically:

```python
class CreateUser(msgspec.Struct):
    username: str
    email: str
    password: str

@api.post("/users")
async def create_user(user: CreateUser):
    return {"id": 1, "username": user.username}
```

### Status codes

Document the default status code:

```python
@api.post("/users", status_code=201)
async def create_user():
    return {"id": 1}
```

## Customizing documentation UIs

By default, all documentation UIs are served automatically. To serve only specific UIs, provide a custom `render_plugins` list:

### Swagger UI only

```python
from django_bolt.openapi import OpenAPIConfig, SwaggerRenderPlugin

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        render_plugins=[SwaggerRenderPlugin(path="/")],
    )
)
```

### ReDoc only

```python
from django_bolt.openapi import OpenAPIConfig, RedocRenderPlugin

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        render_plugins=[RedocRenderPlugin(path="/")],
    )
)
```

### Scalar only

```python
from django_bolt.openapi import OpenAPIConfig, ScalarRenderPlugin

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        render_plugins=[ScalarRenderPlugin(path="/")],
    )
)
```

### Stoplight Elements only

```python
from django_bolt.openapi import OpenAPIConfig, StoplightRenderPlugin

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        render_plugins=[StoplightRenderPlugin(path="/")],
    )
)
```

### RapiDoc only

```python
from django_bolt.openapi import OpenAPIConfig, RapidocRenderPlugin

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        render_plugins=[RapidocRenderPlugin(path="/")],
    )
)
```

### Multiple UIs at custom paths

```python
from django_bolt.openapi import (
    OpenAPIConfig,
    SwaggerRenderPlugin,
    RedocRenderPlugin,
)

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        render_plugins=[
            SwaggerRenderPlugin(path="/"),      # /docs
            RedocRenderPlugin(path="/redoc"),   # /docs/redoc
        ],
    )
)
```

## Raw OpenAPI JSON/YAML

The raw OpenAPI specification is always available at:

- `/docs/openapi.json` - JSON format
- `/docs/openapi.yaml` - YAML format (requires `pyyaml` package)

## Protecting documentation

### Django session authentication

Require Django user login to access docs (redirects to login page):

```python
api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        django_auth=True,  # Requires any logged-in Django user
    )
)
```

For staff-only access, use Django's `staff_member_required`:

```python
from django.contrib.admin.views.decorators import staff_member_required

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        django_auth=staff_member_required,  # Requires staff user
    )
)
```

### API-based authentication

Protect docs with JWT or API key authentication (returns 401/403 instead of redirects):

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        auth=[JWTAuthentication()],
        guards=[IsAuthenticated()],
    )
)
```

For staff-only API access:

```python
from django_bolt.auth import JWTAuthentication, IsStaff

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        version="1.0.0",
        auth=[JWTAuthentication()],
        guards=[IsStaff()],
    )
)
```

### Disabling documentation

Disable documentation in production:

```python
import os

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="My API",
        enabled=os.environ.get("DEBUG", "false").lower() == "true",
    )
)
```

## Parameter documentation

Parameters are documented automatically from function signatures:

```python
@api.get("/search")
async def search(
    q: str,           # Required query parameter
    page: int = 1,    # Optional with default
    limit: int = 20,  # Optional with default
):
    """
    Search for items.

    - **q**: Search query string (required)
    - **page**: Page number (default: 1)
    - **limit**: Items per page (default: 20)
    """
    return {"query": q, "page": page, "limit": limit}
```

## Hiding endpoints

Exclude endpoints from documentation:

```python
@api.get("/internal", include_in_schema=False)
async def internal():
    return {"internal": True}
```

## OpenAPI extensions

The generated OpenAPI spec follows the OpenAPI 3.1.0 specification and includes:

- Path parameters with types
- Query parameters with defaults
- Request body schemas from `msgspec.Struct`
- Response schemas from `response_model`
- Authentication requirements from `auth=` and `guards=`
- Tag grouping
- Operation summaries and descriptions

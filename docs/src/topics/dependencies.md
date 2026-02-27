---
icon: lucide/plug
---

# Dependencies

Django-Bolt provides a dependency injection system using the `Depends` marker. Dependencies let you extract common logic into reusable functions.

## Basic usage

### Creating a dependency

A dependency is any callable (function or class) that returns a value:

```python
from django_bolt import BoltAPI, Depends

api = BoltAPI()

async def get_current_user(request):
    """Dependency that extracts the current user."""
    user_id = request.get("context", {}).get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await User.objects.aget(id=user_id)

@api.get("/profile")
async def get_profile(user=Depends(get_current_user)):
    return {"id": user.id, "username": user.username}
```

### Dependency resolution

When a handler parameter is annotated with `Depends()`, Django-Bolt:

1. Calls the dependency function
2. Passes the result to the handler
3. Caches the result for subsequent uses in the same request

## Dependency patterns

### Request access

Dependencies receive the request dict:

```python
async def get_db_connection(request):
    """Get database connection from request context."""
    return request.get("db_connection")
```

### With parameters

Dependencies can accept the same parameters as handlers:

```python
from typing import Annotated
from django_bolt.param_functions import Header

async def get_api_version(
    x_api_version: Annotated[str, Header()] = "v1"
):
    """Extract API version from header."""
    return x_api_version

@api.get("/data")
async def get_data(version=Depends(get_api_version)):
    if version == "v2":
        return {"format": "new"}
    return {"format": "legacy"}
```

### Authentication dependency

```python
from django_bolt.auth import get_current_user

@api.get("/me")
async def me(user=Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email
    }
```

## Dependency caching

### Default behavior

By default, dependencies are cached per-request:

```python
call_count = 0

async def expensive_operation(request):
    global call_count
    call_count += 1
    # Expensive computation
    return {"count": call_count}

@api.get("/test")
async def test(
    dep1=Depends(expensive_operation),
    dep2=Depends(expensive_operation)  # Same dependency
):
    # expensive_operation is called ONCE, result is reused
    return {"dep1": dep1, "dep2": dep2}
```

### Disabling cache

To force fresh calls, use `use_cache=False`:

```python
@api.get("/fresh")
async def fresh(
    dep=Depends(some_dependency, use_cache=False)
):
    # Always gets fresh result
    return dep
```

## Sync and async dependencies

Django-Bolt supports both sync and async dependencies:

```python
# Async dependency
async def async_dep(request):
    user = await User.objects.aget(id=1)
    return user

# Sync dependency
def sync_dep(request):
    return {"timestamp": time.time()}

@api.get("/mixed")
async def mixed(
    user=Depends(async_dep),
    data=Depends(sync_dep)
):
    return {"user": user.username, "data": data}
```

## Nested dependencies

Dependencies can depend on other dependencies:

```python
async def get_settings(request):
    """Get application settings."""
    return await Settings.objects.afirst()

async def get_feature_flags(settings=Depends(get_settings)):
    """Get feature flags based on settings."""
    return {
        "new_ui": settings.enable_new_ui,
        "beta": settings.beta_features,
    }

@api.get("/features")
async def features(flags=Depends(get_feature_flags)):
    return flags
```

The dependency resolution is:
1. `get_settings` is called first
2. Its result is passed to `get_feature_flags`
3. `get_feature_flags` result is passed to the handler

## Class-based dependencies

Classes can be used as dependencies:

```python
class DatabaseSession:
    def __init__(self, request):
        self.request = request
        self.connection = None

    async def __aenter__(self):
        self.connection = await get_connection()
        return self.connection

    async def __aexit__(self, *args):
        if self.connection:
            await self.connection.close()

@api.get("/data")
async def get_data(db=Depends(DatabaseSession)):
    async with db:
        # Use database connection
        pass
```

## Parameter extraction in dependencies

Dependencies can use the same parameter markers as handlers:

```python
from typing import Annotated
from django_bolt.param_functions import Header, Cookie

async def verify_token(
    authorization: Annotated[str, Header()],
    session_id: Annotated[str, Cookie()]
):
    """Verify both token and session."""
    # Validate authorization header
    # Validate session cookie
    return {"valid": True}

@api.get("/secure")
async def secure_endpoint(auth=Depends(verify_token)):
    return {"status": "authenticated"}
```

## Common dependency patterns

### Database session

```python
async def get_db(request):
    """Provide a database session."""
    from django.db import connection
    await connection.ensure_connection()
    return connection

@api.get("/users")
async def list_users(db=Depends(get_db)):
    return await User.objects.all()[:10]
```

### Pagination

```python
from dataclasses import dataclass

@dataclass
class Pagination:
    page: int
    page_size: int
    offset: int

def get_pagination(page: int = 1, page_size: int = 20) -> Pagination:
    """Extract pagination parameters."""
    return Pagination(
        page=page,
        page_size=min(page_size, 100),  # Max 100 per page
        offset=(page - 1) * page_size
    )

@api.get("/items")
async def list_items(pagination: Pagination = Depends(get_pagination)):
    items = await Item.objects.all()[
        pagination.offset:pagination.offset + pagination.page_size
    ]
    return {"page": pagination.page, "items": items}
```

### Rate limit check

```python
from django_bolt.exceptions import TooManyRequests

async def check_rate_limit(request):
    """Check if request is rate limited."""
    client_ip = request.get("headers", {}).get("x-forwarded-for", "unknown")
    # Check rate limit logic
    if is_rate_limited(client_ip):
        raise TooManyRequests(detail="Rate limit exceeded")
    return True

@api.get("/api/data")
async def get_data(_=Depends(check_rate_limit)):
    return {"data": "value"}
```

### Permission check

```python
async def require_admin(request):
    """Require admin user."""
    user_id = request.get("context", {}).get("user_id")
    if not user_id:
        raise Unauthorized()

    user = await User.objects.aget(id=user_id)
    if not user.is_staff:
        raise Forbidden(detail="Admin access required")

    return user

@api.delete("/users/{user_id}")
async def delete_user(user_id: int, admin=Depends(require_admin)):
    await User.objects.filter(id=user_id).adelete()
    return {"deleted": True}
```

## Dependencies with ViewSets

Dependencies work with class-based views:

```python
from django_bolt import ViewSet

@api.viewset("/items")
class ItemViewSet(ViewSet):
    queryset = Item.objects.all()

    async def list(self, request, pagination=Depends(get_pagination)) -> list[dict]:
        items = []
        queryset = await self.get_queryset()
        async for item in queryset[pagination.offset:pagination.offset + pagination.page_size]:
            items.append({"id": item.id, "name": item.name})
        return items
```

## Testing with dependencies

Override dependencies in tests:

```python
from django_bolt.testing import TestClient

def mock_get_user(request):
    return User(id=1, username="test")

# In tests, the actual dependency is replaced
@api.get("/profile")
async def profile(user=Depends(get_current_user)):
    return {"username": user.username}

# Testing (pseudocode - actual implementation may vary)
with TestClient(api) as client:
    # Override dependency
    response = client.get("/profile")
```

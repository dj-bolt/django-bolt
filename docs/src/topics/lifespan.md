---
icon: lucide/power
---

# Lifespan

Django Bolt supports lifespan events for running code during server startup and shutdown. This follows the same pattern as [Starlette's lifespan](https://www.starlette.io/lifespan/), using an async context manager.

Common use cases:

- Initializing database connection pools
- Warming caches
- Starting background tasks
- Cleaning up resources on shutdown

## Basic usage

Pass an async context manager factory to the `lifespan` parameter of `BoltAPI`. Code before `yield` runs on startup, code after runs on shutdown:

```python
from contextlib import asynccontextmanager
from django_bolt import BoltAPI

@asynccontextmanager
async def lifespan(app):
    # Startup
    print("Starting up...")
    yield
    # Shutdown
    print("Shutting down...")

api = BoltAPI(lifespan=lifespan)
```

The `app` argument is the `BoltAPI` instance.

## Resource management

The context manager pattern guarantees cleanup runs even if the server crashes, making it safer than separate startup/shutdown hooks:

```python
from contextlib import asynccontextmanager
from django_bolt import BoltAPI

@asynccontextmanager
async def lifespan(app):
    # Create resources
    redis = await aioredis.from_url("redis://localhost")
    app._redis = redis

    try:
        yield
    finally:
        # Always cleaned up, even on crash
        await redis.close()

api = BoltAPI(lifespan=lifespan)

@api.get("/cached")
async def get_cached(request):
    value = await request.app._redis.get("key")
    return {"value": value}
```

## Database connection pools

```python
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine
from django_bolt import BoltAPI

@asynccontextmanager
async def lifespan(app):
    engine = create_async_engine("postgresql+asyncpg://localhost/mydb")
    app._engine = engine
    try:
        yield
    finally:
        await engine.dispose()

api = BoltAPI(lifespan=lifespan)
```

## Cache warming

```python
from contextlib import asynccontextmanager
from django_bolt import BoltAPI

@asynccontextmanager
async def lifespan(app):
    # Load frequently accessed data into memory
    from myapp.models import Setting
    app._settings_cache = {
        s.key: s.value
        async for s in Setting.objects.all()
    }
    print(f"Loaded {len(app._settings_cache)} settings")
    yield
    app._settings_cache.clear()

api = BoltAPI(lifespan=lifespan)
```

## Multiple APIs

When multiple `BoltAPI` instances are autodiscovered (multi-app projects), each API's lifespan runs independently. Startup runs in discovery order; shutdown runs in reverse order (LIFO).

```python
# blog/api.py
@asynccontextmanager
async def blog_lifespan(app):
    print("Blog API starting")
    yield
    print("Blog API stopping")

api = BoltAPI(lifespan=blog_lifespan)

# payments/api.py
@asynccontextmanager
async def payments_lifespan(app):
    print("Payments API starting")
    yield
    print("Payments API stopping")

api = BoltAPI(lifespan=payments_lifespan)

# Output on startup:
#   Blog API starting
#   Payments API starting
# Output on shutdown:
#   Payments API stopping
#   Blog API stopping
```

## Testing

The `TestClient` is lifespan-aware. When you enter the `with` block, startup runs. When you exit, shutdown runs. No special setup needed:

```python
from contextlib import asynccontextmanager
from django_bolt import BoltAPI
from django_bolt.testing import TestClient

@asynccontextmanager
async def lifespan(app):
    app._cache = {"ready": True}
    yield
    app._cache.clear()

api = BoltAPI(lifespan=lifespan)

@api.get("/status")
async def status(request):
    return {"ready": request.app._cache.get("ready", False)}

# Lifespan runs automatically
with TestClient(api) as client:
    response = client.get("/status")
    assert response.json() == {"ready": True}
```

The `AsyncTestClient` also supports lifespan:

```python
async with AsyncTestClient(api) as client:
    response = await client.get("/status")
    assert response.json() == {"ready": True}
```

## How it works

- **`runbolt` command**: Lifespan enters before the Rust server starts and exits after it stops. The server runs in a thread while the async event loop manages the lifespan context.
- **`TestClient`**: Lifespan enters on `__enter__` and exits on `__exit__`, using a dedicated event loop that stays alive for the duration of the test.
- **Multi-process mode**: Each process runs its own lifespan independently.

---
icon: lucide/key-round
---

# Authentication

Django-Bolt provides built-in authentication backends that run in Rust for high performance. This guide covers how to set up and use authentication in your API.

## JWT authentication

JWT (JSON Web Token) is the most common authentication method for APIs. Django-Bolt validates JWT tokens in Rust without the Python GIL overhead.

### Basic usage

```python
from django_bolt import BoltAPI
from django_bolt.auth import JWTAuthentication, IsAuthenticated

api = BoltAPI()

@api.get("/profile", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def profile(request):
    return {"user_id": request.user.id}
```

This endpoint:

1. Expects a JWT token in the `Authorization` header: `Bearer <token>`
2. Validates the token signature and expiration
3. Rejects the request with 401 if the token is invalid
4. Populates `request.context` with token claims

!!! warning "`auth` attempts, `guards` enforce"

    `auth=[...]` alone does **not** reject unauthenticated requests — it only *attempts* to validate credentials. If the token is missing, expired, or has an invalid signature, the handler still runs with `request.context = None` and `request.user = AnonymousUser`. This is intentional so that endpoints can support optional authentication (e.g. personalize if logged in, otherwise public).

    To require a valid token, pair `auth` with a guard like `guards=[IsAuthenticated()]`. Without it, invalid credentials fall through silently.

    ```python
    # ❌ NOT protected — invalid/missing tokens still reach the handler
    @api.get("/profile", auth=[JWTAuthentication()])
    async def profile(request):
        print(request.context)  # None when token is invalid/missing
        print(request.user)     # AnonymousUser

    # ✅ Protected — returns 401 when token is invalid/missing
    @api.get("/profile", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
    async def profile(request):
        print(request.context)  # {"user_id": ..., "auth_backend": "jwt", ...}
        print(request.user)     # real User instance (lazy-loaded)
    ```

    See [Permissions](permissions.md) for the full list of guards.

### Creating tokens for users

Use `create_jwt_for_user` to generate tokens for Django users:

```python
from django.contrib.auth import aauthenticate
from django_bolt.auth import create_jwt_for_user
from django_bolt.exceptions import Unauthorized
import msgspec

class LoginRequest(msgspec.Struct):
    username: str
    password: str

@api.post("/auth/token")
async def login(credentials: LoginRequest):
    user = await aauthenticate(
        username=credentials.username,
        password=credentials.password
    )

    if user is None:
        raise Unauthorized(detail="Invalid credentials")

    token = create_jwt_for_user(user, expires_in=3600)  # 1 hour

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 3600
    }
```

The generated token includes:

- `sub` - User's primary key
- `is_staff` - Staff status
- `is_superuser` - Superuser status
- `username` - Username
- `exp` - Expiration timestamp

!!! note "Permissions not included by default"

    Permissions are NOT automatically included in the token. To use `HasPermission` guards, pass permissions via `extra_claims`:

    ```python
    token = create_jwt_for_user(
        user,
        extra_claims={"permissions": list(user.get_all_permissions())}
    )
    ```

### Accessing the authenticated user

Django-Bolt provides lazy user loading via `request.user`:

```python
@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def get_me(request):
    user = request.user  # Lazily loads from database

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email
    }
```

The user is only loaded from the database when you access `request.user`. If you don't need the full user object, use `request.context` which is available without a database query.

### Using dependency injection

Alternatively, use the `get_current_user` dependency:

```python
from django_bolt import Depends
from django_bolt.auth import get_current_user

@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def get_me(user=Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username
    }
```

### JWT configuration options

Customize JWT validation:

```python
JWTAuthentication(
    secret="your-secret-key",    # Default: Django's SECRET_KEY
    algorithms=["HS256"],        # Allowed algorithms
    header="authorization",      # Header name
    audience="your-app",         # Required audience claim
    issuer="your-issuer",        # Required issuer claim
)
```

Supported algorithms:

- `HS256`, `HS384`, `HS512` - HMAC with SHA-2
- `RS256`, `RS384`, `RS512` - RSA with SHA-2
- `ES256`, `ES384`, `ES512` - ECDSA with SHA-2

## API key authentication

!!! info "In Development"

    API key permissions (`key_permissions` parameter) are in development. Basic API key validation works, but per-key permissions are not yet finalized.

For service-to-service communication, use API key authentication:

```python
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated

api_keys = {"sk-prod-123abc", "sk-prod-456def"}

@api.get(
    "/internal",
    auth=[APIKeyAuthentication(api_keys=api_keys)],
    guards=[IsAuthenticated()]
)
async def internal_endpoint(request):
    return {"status": "authorized"}
```

API keys are sent in the `X-API-Key` header by default.

### API key permissions (In Development)

Assign different permissions to different keys:

```python
key_permissions = {
    "sk-admin-key": {"admin.read", "admin.write"},
    "sk-reader-key": {"admin.read"},
}

APIKeyAuthentication(
    api_keys=set(key_permissions.keys()),
    key_permissions=key_permissions
)
```

### Custom header

Use a different header for API keys:

```python
APIKeyAuthentication(
    api_keys=api_keys,
    header="Authorization"  # Or any custom header
)
```

## Session authentication

Django-Bolt integrates with Django's session-based authentication via middleware. This is ideal when you're already using Django's auth system or need browser-based authentication with cookies.

### Setup

Enable Django middleware to use sessions:

```python
from django_bolt import BoltAPI

# Load session and auth middleware from Django settings
api = BoltAPI(django_middleware=True)

# Or explicitly specify middleware
api = BoltAPI(django_middleware=[
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
])
```

### Login and logout

Use Django's async login/logout functions:

```python
from typing import Annotated
from django.contrib.auth import alogin, alogout
from django.contrib.auth.models import User
from django_bolt import Request
from django_bolt.params import Form

@api.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    user = await User.objects.filter(username=username).afirst()
    if user and user.check_password(password):
        await alogin(request, user)
        return {"status": "logged in", "username": user.username}
    return {"status": "invalid credentials"}

@api.post("/logout")
async def logout(request: Request):
    await alogout(request)
    return {"status": "logged out"}
```

### Accessing the authenticated user

Use `request.auser()` for async user access:

```python
@api.get("/me")
async def me(request: Request):
    user = await request.auser()
    if user.is_authenticated:
        return {"username": user.username, "email": user.email}
    return {"anonymous": True}
```

### Working with session data

Django sessions support async operations. Use `aget`, `aset`, and other async methods:

```python
@api.post("/preferences")
async def save_preferences(request: Request, theme: str = "light"):
    session = request.session

    # Async read with default
    visits = await session.aget("visit_count", 0)

    # Async write
    await session.aset("visit_count", visits + 1)
    await session.aset("theme", theme)

    return {"visits": visits + 1, "theme": theme}

@api.get("/preferences")
async def get_preferences(request: Request):
    session = request.session
    return {
        "theme": await session.aget("theme", "light"),
        "visits": await session.aget("visit_count", 0),
    }
```

### Async session methods

| Method | Description |
|--------|-------------|
| `await session.aget(key, default)` | Get a value from the session |
| `await session.aset(key, value)` | Set a value in the session |
| `await session.apop(key, default)` | Remove and return a value |
| `await session.akeys()` | Get all session keys |
| `await session.aitems()` | Get all session items |
| `await session.aflush()` | Delete session and create new one |
| `await session.acycle_key()` | Regenerate session key (keeps data) |
| `session.session_key` | Get session key (sync, no DB access) |
| `session.clear()` | Clear session data (sync, no DB access) |

!!! warning "Use async methods in async handlers"
    Always use async methods (`aget`, `aset`, etc.) in async handlers. Using sync methods like `session["key"]` or `session.get()` raises `SynchronousOnlyOperation`.

### When to use session vs JWT

| Feature | Session Auth | JWT Auth |
|---------|-------------|----------|
| Storage | Server-side (DB/cache) | Client-side (token) |
| Logout | Immediate (delete session) | Requires revocation store |
| Scalability | Requires shared session store | Stateless, scales easily |
| Use case | Browser apps, traditional web | APIs, mobile apps, SPAs |

### Using Django decorators

With `django_middleware=True`, Django's authentication decorators work directly:

```python
from django.contrib.auth.decorators import login_required, permission_required

api = BoltAPI(django_middleware=True)

@api.get("/dashboard")
@login_required
async def dashboard(request: Request):
    """Protected by @login_required - redirects to login if not authenticated."""
    user = await request.auser()
    return {"welcome": user.username}

@api.get("/admin/users")
@permission_required("auth.view_user", raise_exception=True)
async def admin_users(request: Request):
    """Protected by @permission_required - returns 403 without permission."""
    users = await User.objects.all().avalues_list("username", flat=True)
    return {"users": list(users)}

@api.get("/reports")
@permission_required("reports.view_report", login_url="/login/")
async def reports(request: Request):
    """Redirects to custom login URL if no permission."""
    return {"reports": [...]}
```

Available Django decorators:

| Decorator | Description |
|-----------|-------------|
| `@login_required` | Redirects to login page if not authenticated |
| `@login_required(login_url="/custom/")` | Custom login URL |
| `@permission_required("app.perm")` | Requires specific permission |
| `@permission_required("app.perm", raise_exception=True)` | Returns 403 instead of redirect |
| `@user_passes_test(lambda u: u.is_staff)` | Custom test function |

!!! tip "Decorator order"
    Place Django decorators **after** the route decorator:
    ```python
    @api.get("/path")      # First: route decorator
    @login_required        # Second: auth decorator
    async def view():
        ...
    ```

## Combining authentication methods

Accept multiple authentication methods:

```python
@api.get(
    "/data",
    auth=[JWTAuthentication(), APIKeyAuthentication(api_keys=api_keys)],
    guards=[IsAuthenticated()]
)
async def get_data(request):
    backend = request.context.get("auth_backend")
    return {"authenticated_via": backend}
```

Django-Bolt tries each backend in order until one succeeds.

## Authentication context

After successful authentication, `request.context` contains:

```python
@api.get("/context", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def show_context(request):
    context = request.context

    return {
        "user_id": context.get("user_id"),
        "is_staff": context.get("is_staff"),
        "is_superuser": context.get("is_superuser"),
        "auth_backend": context.get("auth_backend"),  # "jwt" or "api_key"
        "permissions": context.get("permissions", []),
        "auth_claims": context.get("auth_claims", {}),  # JWT only
    }
```

## Token revocation

For logout functionality, Django-Bolt supports token revocation stores.
When you pass `revocation_store=` to `JWTAuthentication`, every authenticated
request is checked against the store before reaching your handler — revoked
tokens are automatically rejected with `401 Unauthorized`. You don't need
to call `is_revoked()` manually.

!!! note "JTI is required"

    Configuring a `revocation_store` auto-enables `require_jti=True` on the
    backend. Tokens without a `jti` claim are rejected at auth time. Make
    sure your token issuer adds one — `create_jwt_for_user(...,
    extra_claims={"jti": uuid.uuid4().hex})` is the simplest way.

!!! tip "Always pass the token's `exp`"

    `revoke()` accepts an `exp` keyword argument. Pass the token's own
    `exp` claim so the revocation entry expires exactly when the token
    would have anyway — no security gap, no wasted storage. Without
    `exp`, the entry falls back to the store's `default_ttl` (7 days
    by default).

### In-memory revocation (development)

```python
from django_bolt.auth import JWTAuthentication, InMemoryRevocation, IsAuthenticated

store = InMemoryRevocation()
jwt_auth = JWTAuthentication(revocation_store=store)

@api.post("/logout", auth=[jwt_auth], guards=[IsAuthenticated()])
async def logout(request):
    claims = request["context"]["auth_claims"]
    await store.revoke(claims["jti"], exp=claims.get("exp"))
    return {"status": "logged out"}
```

After this handler runs, any further request carrying the same token
will be rejected with `401 Unauthorized` — automatically, no per-handler
check needed.

!!! warning "Single process only"

    `InMemoryRevocation` keeps state in process memory and does not
    survive restarts or share state across workers. Use it for
    development and tests only. Pick `DjangoCacheRevocation` or
    `DjangoORMRevocation` in production.

### Django cache revocation (production)

```python
from django_bolt.auth import DjangoCacheRevocation

store = DjangoCacheRevocation(
    cache_alias="default",
    key_prefix="revoked_tokens:",
    default_ttl=86400 * 7,  # 7 days — fallback when revoke() is called without `exp`
)
```

Works with any Django cache backend (Redis, Memcached, locmem, etc.).
Recommended for multi-process deployments — entries are visible to all
workers via the shared cache.

### Django ORM revocation

```python
from django_bolt.auth import DjangoORMRevocation

store = DjangoORMRevocation(
    model="myapp.RevokedToken",  # 'app_label.ModelName' — exactly two parts
    default_ttl=86400 * 7,
)
```

Requires a model with `jti` (unique, indexed) and `expires_at`
(indexed) fields. A periodic cleanup task should delete rows where
`expires_at < now()`. Slower than cache-based stores; only use when you
don't have a cache layer.

### Per-call vs per-instance TTL

`revoke()` resolves the entry's lifetime in this order:

1. **`exp` keyword arg** — derived as `max(0, exp - now)`. Use this whenever
   you have the token's full claims (i.e. always, in a logout handler).
2. **`store.default_ttl`** — set on the constructor. Used when `revoke()`
   is called without `exp` (e.g. an admin action that revokes by JTI).
3. **`_DEFAULT_TTL_SECONDS`** — module-level fallback (7 days) if neither
   of the above is supplied.

## Endpoints without authentication

Use `AllowAny` to explicitly allow unauthenticated access:

```python
from django_bolt.auth import AllowAny

@api.get("/public", guards=[AllowAny()])
async def public_endpoint():
    return {"message": "Anyone can access this"}
```

## Global authentication

Configure default authentication and guards for all endpoints in `settings.py`:

```python
# settings.py
from django_bolt.auth import JWTAuthentication, IsAuthenticated

# Default authentication backends for all endpoints
BOLT_AUTHENTICATION_CLASSES = [
    JWTAuthentication(),
]

# Default permission guards for all endpoints
BOLT_DEFAULT_PERMISSION_CLASSES = [
    IsAuthenticated(),
]
```

!!! warning "Security Notice"

    If `BOLT_DEFAULT_PERMISSION_CLASSES` is not set, it defaults to `[AllowAny()]` which means **all endpoints are publicly accessible**. Always configure both settings in production.

When configured, all endpoints require authentication by default:

```python
# Uses global auth + guards - requires valid JWT
@api.get("/profile")
async def profile(request):
    return {"user_id": request.user.id}

# Override guards for public endpoint
@api.get("/public", guards=[AllowAny()])
async def public():
    return {"message": "Anyone can access"}

# Override auth for specific endpoint
@api.get("/api-only", auth=[APIKeyAuthentication(api_keys={"secret"})])
async def api_only(request):
    return {"status": "ok"}
```

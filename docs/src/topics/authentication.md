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

### Custom user query

By default `request.user` runs `User.objects.get(pk=<sub claim>)`. To use a
different query — `select_related`, `defer`, a custom user model, lookup by
username — subclass the backend and override `get_user`:

```python
class MyJWT(JWTAuthentication):
    async def get_user(self, user_id, auth_context):
        return await (
            User.objects
            .select_related("track", "member")
            .defer("track__image", "member__joined_at")
            .aget(id=user_id)
        )

@api.get("/tasks", auth=[MyJWT()], guards=[IsAuthenticated()])
async def tasks(request):
    user = request.user  # loaded with your query
    ...
```

Overriding the async `get_user` or the sync `get_user_sync` both work —
either alone is enough. If you define both, sync handlers use
`get_user_sync` directly (no event-loop overhead) and it is also preferred
for async handlers via a worker thread.

The override is scoped to the routes that use that backend instance —
routes authenticated with a plain `JWTAuthentication` keep the default
pk-based query, even in the same app.

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
    cookie=None,                 # Cookie name to read the token from
    audience="your-app",         # Required audience claim
    issuer="your-issuer",        # Required issuer claim
)
```

Supported algorithms:

- `HS256`, `HS384`, `HS512` - HMAC with SHA-2 (`secret` is the shared secret)
- `RS256`, `RS384`, `RS512` - RSA with SHA-2 (`secret` is a PEM public key)
- `PS256`, `PS384`, `PS512` - RSA-PSS with SHA-2 (`secret` is a PEM public key)
- `ES256`, `ES384` - ECDSA with SHA-2 (`secret` is a PEM public key)
- `EdDSA` - Ed25519 (`secret` is a PEM public key)

All algorithms configured on a single backend must use the same kind of
key; you cannot mix `HS256` with `RS256`. Configuration errors — an
unknown algorithm name, algorithms from different key families, or a key
that is not valid PEM — stop the server at startup with a descriptive
error, rather than silently rejecting every token at runtime.

### Verifying tokens from an identity provider

If your users sign in through an external identity provider such as
Clerk, Auth0, or Okta, the provider signs its tokens with an asymmetric
algorithm and publishes the corresponding public key. Pass the key with
`public_key=` and name the matching algorithm:

```python
JWTAuthentication(
    public_key=CLERK_PEM_PUBLIC_KEY,  # from the provider's dashboard
    algorithms=["RS256"],
    issuer="https://your-app.clerk.accounts.dev",
)
```

Most providers also publish their keys as a JSON Web Key Set and rotate
them periodically, identifying each key with a `kid` header on the token.
To use the key set instead of a single key, point `jwks_url=` at the
provider's JWKS endpoint:

```python
JWTAuthentication(
    jwks_url="https://your-tenant.auth0.com/.well-known/jwks.json",
    algorithms=["RS256"],
    issuer="https://your-tenant.auth0.com/",
    audience="https://api.example.com",
)
```

`jwks_url` must be an absolute HTTPS URL and must be paired with both
`issuer=` and `audience=`. This binds accepted tokens to the intended
provider and API instead of accepting another application's token merely
because the provider signed it. The key set is fetched once when the
server starts and parsed into
per-`kid` verification keys; on each request, the token's `kid` header
selects the key. If the provider rotates its signing keys to a new
`kid`, restart the server to pick up the new set.

To supply a key set directly rather than fetching it — in tests, or in
deployments without network access — pass the document itself as `jwks=`,
either as a dict or a JSON string.

### Cookie-based tokens

For browser clients that store the JWT in a cookie (e.g. an `HttpOnly`
`access_token` cookie), pass `cookie=` with the cookie name:

```python
@api.get(
    "/profile",
    auth=[JWTAuthentication(cookie="access_token")],
    guards=[IsAuthenticated()],
)
async def profile(request):
    return {"user_id": request["context"]["user_id"]}
```

When `cookie=` is set, the token is read from the named cookie only — the
`Authorization` header is ignored for that backend. The cookie value is the
raw token, no `Bearer` prefix needed. Extraction and validation both
happen in Rust, without acquiring the GIL.

To serve both browser (cookie) and API (bearer header) clients on the same
endpoint, register two backends — they're tried in order:

```python
auth=[
    JWTAuthentication(cookie="access_token"),
    JWTAuthentication(),
]
```

### Cross-site request forgery protection

Browsers attach cookies to requests automatically, including requests
made from other sites. Django-Bolt endpoints do not run Django's
`CsrfViewMiddleware`, so cookie authentication provides its own CSRF
protection, enabled by default.

When a backend successfully authenticates from a cookie, any
state-changing request (a method other than `GET`, `HEAD`, `OPTIONS`, or
`TRACE`) must show it originated from your own site. The check runs in
Rust after selecting the accepted credential. The browser-set
`Sec-Fetch-Site` header
is used when present; otherwise the origin of the `Origin` (or
`Referer`) header — scheme and host — is compared against the request's
effective scheme (honoring `X-Forwarded-Proto` behind a proxy) and
`Host`. HTTP and HTTPS are different origins, so an
`Origin: http://example.com` never authorizes an HTTPS request to
`example.com`. Requests that fail the check receive a `403 Forbidden`
response.

The check applies only when the accepted credential came from the cookie
backend. Requests authenticated some other way — for example, through
the header backend of a route that registers both backends as above —
are not affected, even if the request also carries a stale cookie.

!!! note "Non-browser clients"

    Clients other than browsers do not usually send `Sec-Fetch-Site`,
    `Origin`, or `Referer`, so their state-changing requests with the
    cookie will be rejected. Have such clients send the token in the
    `Authorization` header instead. If that isn't possible, disable the
    check with `csrf=False` and provide equivalent protection yourself.

## Access and refresh tokens

Issuing a single long-lived token means a stolen token stays valid until
it expires. The usual remedy is the dual-token pattern: a short-lived
**access token** sent on every request, and a long-lived **refresh
token**, sent only to a rotation endpoint, that is exchanged for fresh
access tokens as they expire. Django-Bolt implements this pattern with
`create_token_pair()`, `rotate_refresh_token()`, and
`set_token_cookies()`.

### Issuing a pair

Call `create_token_pair()` once the user has proved who they are — by
password, magic link, OAuth callback, or any other flow:

```python
from django.contrib.auth import aauthenticate
from django_bolt.auth import create_token_pair, set_token_cookies
from django_bolt.exceptions import Unauthorized
from django_bolt.responses import JSON

@api.post("/auth/login")
async def login(credentials: LoginRequest):
    user = await aauthenticate(
        username=credentials.username,
        password=credentials.password,
    )
    if user is None:
        raise Unauthorized(detail="Invalid credentials")

    pair = create_token_pair(user, method="pwd")
    response = JSON({"status": "ok"})
    return set_token_cookies(response, pair, refresh_path="/auth/refresh")
```

`set_token_cookies()` attaches both tokens as `HttpOnly`, `Secure`,
`SameSite=Lax` cookies. Pass `refresh_path=` with the path of your
rotation endpoint: the refresh cookie is then scoped to that path, so
the browser sends it only when refreshing, never on ordinary API
requests.

Access tokens live for 15 minutes by default and refresh tokens for 7
days; change these with `access_ttl=` and `refresh_ttl=`. Add extra
claims with `claims=`. The lifecycle claims the pair is built on —
`sub`, `iat`, `exp`, `typ`, `jti`, `fam`, `oat`, and `ver` — are
reserved, and attempting to override one raises `ValueError`.

### The rotation endpoint

The rotation endpoint authenticates with `token_type="refresh"`, which
accepts only tokens carrying the claim `typ: "refresh"`. Conversely,
every route configured *without* `token_type=` rejects refresh tokens,
so a refresh token can never authenticate a normal endpoint. Both checks
run in Rust before your handler is called.

```python
from django_bolt.auth import (
    DjangoCacheRevocation,
    IsAuthenticated,
    JWTAuthentication,
    TokenRotationError,
    rotate_refresh_token,
    set_token_cookies,
)
from django_bolt.exceptions import Unauthorized
from django_bolt.responses import JSON

store = DjangoCacheRevocation()

@api.post(
    "/auth/refresh",
    auth=[JWTAuthentication(cookie="refresh_token", token_type="refresh")],
    guards=[IsAuthenticated()],
)
async def refresh(request):
    claims = request["context"]["auth_claims"]
    try:
        pair = await rotate_refresh_token(claims, store=store)
    except TokenRotationError:
        raise Unauthorized(detail="Invalid refresh token")

    response = JSON({"status": "ok"})
    return set_token_cookies(response, pair, refresh_path="/auth/refresh")
```

By the time your handler runs, the token's signature, expiry, and type
have already been verified in Rust. `rotate_refresh_token()` performs
the stateful checks against the revocation store, atomically consumes the
old refresh token, and returns a new pair. A custom store used for full
rotation must implement `consume(jti, *, exp=None)` as an atomic
check-and-set and support family revocation; rotation fails closed if it
cannot. If the validating `JWTAuthentication` uses a non-default `leeway`,
pass the same value to `rotate_refresh_token()` so consumed tokens remain
blocked throughout the validator's clock-skew window.

Rotating the refresh token on every use provides **reuse detection**.
Each pair belongs to a rotation family (the `fam` claim); a refresh
token presented again after it has been rotated out is treated as
stolen, and the whole family is revoked, ending the session for whoever
holds any of its tokens. If you prefer to issue only new access tokens
and leave the refresh token in place, pass `rotate=False`; this mode
gives up reuse detection.

Custom claims are not copied from the old token during rotation, since
they may have gone stale since the original login. Re-derive any claims
you need and pass them with `claims=`.

!!! note "Return a generic error"

    Catch `TokenRotationError` and return a uniform `401`. The exception
    message records why rotation failed — revoked, reused, stale
    version, or an expired session — and that detail should not reach
    the client.

### Logging a user out everywhere

Revocation stores keep a version number for each user, and every token
pair records the version it was minted under (the `ver` claim). Calling
`bump_user_version()` increments the stored version; refresh tokens
minted before the bump fail rotation from then on:

```python
@api.post("/auth/logout-all", auth=[jwt_auth], guards=[IsAuthenticated()])
async def logout_all(request):
    await store.bump_user_version(request["context"]["user_id"])
    return {"status": "logged out everywhere"}
```

This is a single O(1) write; you do not need to find and revoke each
outstanding token.

!!! note "Access tokens run out their lifetime"

    Access tokens are deliberately not checked against the store on each
    request — that is what makes them cheap to validate. A user who is
    "logged out everywhere" can therefore keep using an existing access
    token until it expires. Keep access lifetimes short; with the
    default 15 minutes, that is the longest such a token can survive.

### Limiting total session length

Every pair carries an `oat` (origin auth time) claim recording when the
user originally authenticated. The value is copied unchanged across
rotations, so it can bound the total length of a session no matter how
many times the session refreshes:

```python
pair = await rotate_refresh_token(
    claims,
    store=store,
    max_session_lifetime=86400 * 30,  # require sign-in again after 30 days
)
```

Once the cap is exceeded, rotation raises `TokenRotationError` and the
user must sign in again. Two related guarantees hold whenever the cap is
configured: a refresh token *without* an `oat` claim (for example, one
minted outside this module) fails closed rather than being re-minted
with a fresh origin time, and the access token issued by a rotation is
clamped to the session's remaining lifetime so it can never outlive the
cap.

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

Basic revocation works with any real Django cache backend. Refresh
rotation and version bumps require atomic `add`/`incr`; use Redis or
Memcached for multi-process production deployments. LocMem is suitable
only for a single process. File-based, database, dummy, and other
non-atomic caches are rejected for these operations. Configure the cache so security entries are not
evicted before their TTL.

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

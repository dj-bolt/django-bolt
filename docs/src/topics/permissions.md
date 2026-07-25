---
icon: lucide/shield-check
---

# Permissions

Django-Bolt uses "guards" to control access to endpoints. Guards are permission checks that run in Rust after authentication but before your handler is called.

There are exactly three guards:

| Guard | Meaning |
|-------|---------|
| `IsAuthenticated()` | The request must carry a valid credential (else 401) |
| `AllowAny()` | Explicitly public; overrides global default guards |
| `Requires(claim, *values, all_of=...)` | **The** permission check — one primitive for roles, permissions, tenancy, flags |

Every guard compiles to a native Rust check at registration — the claim name and expected values are extracted from Python exactly once, and request-time guard evaluation never touches the GIL.

!!! warning "Guards enforce, `auth` doesn't"

    `auth=[...]` only *attempts* authentication — it never rejects a request on its own. Enforcement (401/403) is the job of guards. An endpoint declared with just `auth=[JWTAuthentication()]` and no guards will happily serve requests with missing or invalid tokens, and `request.context` will be `None`. Always pair `auth` with at least `guards=[IsAuthenticated()]` when you want to require login. See [Authentication](authentication.md#jwt-authentication) for details.

## IsAuthenticated

Requires a valid authentication token:

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated

@api.get("/profile", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def profile(request):
    return {"user_id": request.context["user_id"]}
```

Returns 401 Unauthorized if authentication fails.

## AllowAny

Explicitly allows any request, bypassing global default guards:

```python
from django_bolt.auth import AllowAny

@api.get("/public", guards=[AllowAny()])
async def public():
    return {"message": "Anyone can see this"}
```

## Requires

`Requires` checks a token claim against expected values:

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated, Requires

Requires("tenant_id")                        # claim must exist
Requires("role", "client")                   # equals (or list contains)
Requires("role", "client", "vip")            # any of (OR)
Requires("is_staff", True)                   # boolean claim
Requires("permissions", "blog.add_article")  # Django-style permission
Requires("permissions", all_of=["blog.add_article", "blog.change_article"])  # AND
```

Give reusable checks a name by assignment — no subclassing:

```python
IsClient = Requires("role", "client")
IsStaff = Requires("is_staff", True)
IsSuperuser = Requires("is_superuser", True)

@api.get("/orders", auth=[JWTAuthentication()], guards=[IsAuthenticated(), IsClient])
async def list_orders():
    return {"orders": [...]}
```

Issue tokens carrying the claims your guards read:

```python
from django_bolt.auth import create_jwt_for_user

token = create_jwt_for_user(user, extra_claims={"role": "client"})
```

### Matching semantics

- **Positional values are OR** — the claim must match at least one.
- **`all_of` is AND** — the claim (a list, e.g. `permissions`) must contain every value. Positional values and `all_of` are mutually exclusive.
- **Scalar claims match by equality; list claims by membership** — `Requires("roles", "client")` passes when `roles: ["beta", "client"]`.
- **No values means presence** — the claim must exist and be non-null. A boolean claim must be `true`: `Requires("is_admin")` rejects a token carrying `is_admin: false`.
- **Value types**: `str`, `int`, `bool` (and `float`). Anything else is rejected at registration.
- **Standard claims are guardable too**: `sub`, `iss`, `aud`, `typ`, `is_staff`, `is_superuser`, ... — not just your extra claims.

### The `permissions` claim

`Requires("permissions", ...)` reads the **unified permission set**, which is populated from the JWT `permissions` claim *or* from `key_permissions` for API-key auth — the same guard works for every backend:

```python
from django_bolt.auth import APIKeyAuthentication

@api.get(
    "/reports",
    auth=[APIKeyAuthentication(api_keys={"key-1"}, key_permissions={"key-1": ["reports.view"]})],
    guards=[Requires("permissions", "reports.view")],
)
async def reports(): ...
```

All other claims come from the token, so backends that carry no claims (API keys) can never satisfy them.

### Status codes

`401` when the request is unauthenticated, `403` when the claim is missing or doesn't match. Guards evaluate in declaration order and short-circuit on the first verdict.

### Registration is strict

Anything that can't be compiled fails startup instead of silently leaving the route open: an empty claim name, non-scalar values, mixing positional values with `all_of`, or subclassing `BasePermission` (not a thing — name a `Requires` instance instead) all raise `ImproperlyConfigured`.

## Combining guards

Use multiple guards for layered security:

```python
@api.post(
    "/admin/settings",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated(), Requires("is_staff", True), Requires("permissions", "core.change_settings")]
)
async def update_settings():
    return {"updated": True}
```

Guards are checked in order. The request is rejected as soon as any guard fails.

## Permissions in JWT tokens

Guards run in Rust without database access, so all permission data must be embedded in the JWT token itself.

### How it works

1. When you create a JWT token, you include the user's claims (permissions, role, ...) in the payload
2. The Rust layer validates the token and extracts the claims
3. Guards check against the extracted claims - no database queries

### Creating tokens with permissions

The `create_jwt_for_user()` function automatically includes `is_staff` and `is_superuser`, but **everything else must be passed explicitly** via `extra_claims`:

```python
from django_bolt.auth import create_jwt_for_user

# Basic token - includes is_staff, is_superuser, but NOT permissions
token = create_jwt_for_user(user, expires_in=3600)

# Token with permissions and a role
token = create_jwt_for_user(
    user,
    expires_in=3600,
    extra_claims={
        "permissions": ["blog.add_article", "blog.change_article"],
        "role": "client",
    }
)
```

### Loading permissions from Django

To include a user's Django permissions in the token:

```python
from django_bolt.auth import create_jwt_for_user

def create_token_with_permissions(user):
    # Get all permissions for the user (from groups and direct assignments)
    permissions = list(user.get_all_permissions())

    return create_jwt_for_user(
        user,
        expires_in=3600,
        extra_claims={"permissions": permissions}
    )
```

### Token claims reference

| Claim | Included By Default | Example Guard |
|-------|---------------------|---------------|
| `is_staff` | Yes | `Requires("is_staff", True)` |
| `is_superuser` | Yes | `Requires("is_superuser", True)` |
| `permissions` | No (use `extra_claims`) | `Requires("permissions", "blog.add_article")` |
| anything else | No (use `extra_claims`) | `Requires("role", "client")` |

## Per-route authentication and guards

Authentication and guards are specified per-route using the `auth` and `guards` parameters:

```python
from django_bolt.auth import JWTAuthentication, IsAuthenticated, AllowAny

# Protected endpoint
@api.get("/data", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def get_data():
    return {"protected": True}

# Public endpoint
@api.get("/health", guards=[AllowAny()])
async def health():
    return {"status": "ok"}
```

## When to use a dependency instead

A guard is a claim comparison — it can't `await`, query the database, or see the request body. For rules that need any of that, use a dependency (or a check in the handler, below):

```python
from django_bolt import Depends
from django_bolt.exceptions import HTTPException

async def require_active_subscription(request):
    user_id = request.context.get("user_id")
    if not await Subscription.objects.filter(user_id=user_id, active=True).aexists():
        raise HTTPException(status_code=403, detail="Subscription required")

@api.get("/premium", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def premium(_=Depends(require_active_subscription)):
    return {"ok": True}
```

## Runtime permission checks

For complex permission logic, perform checks in your handler:

```python
from django_bolt.exceptions import Forbidden

@api.delete(
    "/articles/{article_id}",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()]
)
async def delete_article(request, article_id: int):
    article = await Article.objects.aget(id=article_id)

    # Check if user owns the article or is admin
    user = request.user

    if article.author_id != user.id and not user.is_superuser:
        raise Forbidden(detail="You can only delete your own articles")

    await article.adelete()
    return {"deleted": article_id}
```

## Performance

Guards run in Rust before your Python handler is called. This means:

- Invalid requests are rejected without Python GIL overhead
- Authentication and authorization happen in a single pass
- Your handler only runs for authorized requests

This includes `Requires`: its claim name and values are extracted from Python once at registration, so request-time evaluation is fully native.

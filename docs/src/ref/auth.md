---
icon: lucide/lock
---

# Authentication Reference

This page documents all authentication and authorization classes.

## Authentication backends

### JWTAuthentication

JWT token authentication.

```python
from django_bolt.auth import JWTAuthentication

JWTAuthentication(
    secret=None,              # JWT secret (default: Django SECRET_KEY)
    algorithms=["HS256"],     # Allowed algorithms
    header="authorization",   # Header name
    cookie=None,              # Cookie name to read the token from
    audience=None,            # Required audience claim
    issuer=None,              # Required issuer claim
    revocation_store=None,    # Token revocation store
)
```

#### Parameters

| Parameter          | Type            | Default           | Description             |
| ------------------ | --------------- | ----------------- | ----------------------- |
| `secret`           | `str`           | Django SECRET_KEY | HMAC secret, or PEM public key for asymmetric algorithms |
| `algorithms`       | `list[str]`     | `["HS256"]`       | Allowed JWT algorithms (token `alg` must be in this list) |
| `header`           | `str`           | `"authorization"` | Header containing token |
| `cookie`           | `str`           | `None`            | Cookie containing token (replaces `header` when set) |
| `audience`         | `str`           | `None`            | Required `aud` claim    |
| `issuer`           | `str`           | `None`            | Required `iss` claim    |
| `revocation_store` | RevocationStore | `None`            | Token revocation store  |

#### Supported algorithms

- HMAC: `HS256`, `HS384`, `HS512` — `secret` is the shared secret
- RSA: `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512` — `secret` is a PEM-encoded RSA public key
- ECDSA: `ES256`, `ES384` — `secret` is a PEM-encoded EC public key
- EdDSA: `EdDSA` (Ed25519) — `secret` is a PEM-encoded Ed25519 public key

All algorithms in one backend must share a key family (you cannot mix
`HS256` with `RS256`). Invalid configuration — an unknown algorithm name,
mixed families, or a malformed PEM — fails at server startup with a
descriptive error rather than rejecting every token at runtime.

Verifying tokens from an external identity provider (Clerk, Auth0, etc.)
works by passing the provider's PEM public key:

```python
JWTAuthentication(
    secret=CLERK_PEM_PUBLIC_KEY,   # from the provider's dashboard
    algorithms=["RS256"],
    issuer="https://your-app.clerk.accounts.dev",
)
```

#### Validation behavior

All validation runs in Rust on the hot path, using a verification key
prebuilt at startup:

- The token's `alg` header must be in `algorithms`; anything else is
  rejected before signature verification (prevents algorithm-confusion
  attacks). JOSE header extension parameters of any JSON type are
  tolerated per RFC 7515 §4.1.11 (Clerk and Auth0 emit non-string ones).
- `exp` is required; `exp`/`nbf` are checked with 60 seconds of
  clock-skew leeway.
- `aud` may be a string or an array. It is only validated when
  `audience` is configured; tokens carrying an `aud` claim are not
  rejected just because no audience was configured.
- When `issuer` is configured, tokens missing `iss` are rejected.

### APIKeyAuthentication

!!! info "In Development"

    API key permissions (`key_permissions` parameter) are in development. Basic API key validation works, but per-key permissions are not yet finalized.

API key authentication.

```python
from django_bolt.auth import APIKeyAuthentication

APIKeyAuthentication(
    api_keys={"key1", "key2"},
    header="x-api-key",
    key_permissions={
        "key1": {"read", "write"},
        "key2": {"read"},
    },
)
```

#### Parameters

| Parameter         | Type       | Default       | Description                |
| ----------------- | ---------- | ------------- | -------------------------- |
| `api_keys`        | `set[str]` | `set()`       | Valid API keys             |
| `header`          | `str`      | `"x-api-key"` | Header containing key      |
| `key_permissions` | `dict`     | `None`        | Key to permissions mapping |

## Permission guards

### AllowAny

Allow any request.

```python
from django_bolt.auth import AllowAny

@api.get("/public", guards=[AllowAny()])
```

### IsAuthenticated

Require valid authentication.

```python
from django_bolt.auth import IsAuthenticated

@api.get("/private", guards=[IsAuthenticated()])
```

Returns 401 if not authenticated.

### IsAdminUser

Require superuser status.

```python
from django_bolt.auth import IsAdminUser

@api.get("/admin", guards=[IsAdminUser()])
```

Returns 403 if not superuser.

### IsStaff

Require staff status.

```python
from django_bolt.auth import IsStaff

@api.get("/staff", guards=[IsStaff()])
```

Returns 403 if not staff.

### HasPermission

Require a specific permission.

```python
from django_bolt.auth import HasPermission

@api.get("/articles", guards=[HasPermission("blog.view_article")])
```

### HasAnyPermission

Require any of the specified permissions.

```python
from django_bolt.auth import HasAnyPermission

@api.get("/content", guards=[HasAnyPermission(["blog.view_article", "blog.add_article"])])
```

### HasAllPermissions

Require all specified permissions.

```python
from django_bolt.auth import HasAllPermissions

@api.delete("/articles/{id}", guards=[HasAllPermissions(["blog.delete_article", "blog.change_article"])])
```

## Token utilities

### create_jwt_for_user

Create a JWT token for a Django user.

```python
from django_bolt.auth import create_jwt_for_user

token = create_jwt_for_user(user, expires_in=3600)
```

#### Parameters

| Parameter      | Type   | Default  | Description                  |
| -------------- | ------ | -------- | ---------------------------- |
| `user`         | User   | required | Django user instance         |
| `expires_in`   | `int`  | `3600`   | Token lifetime in seconds    |
| `extra_claims` | `dict` | `None`   | Additional claims to include |

#### Token claims

The generated token automatically includes:

| Claim          | Description                    |
| -------------- | ------------------------------ |
| `sub`          | User's primary key (as string) |
| `is_staff`     | Staff status                   |
| `is_superuser` | Superuser status               |
| `username`     | Username                       |
| `email`        | Email (if available)           |
| `exp`          | Expiration timestamp           |
| `iat`          | Issued at timestamp            |

**Note:** Permissions are NOT automatically included. Pass them via `extra_claims`:

```python
token = create_jwt_for_user(
    user,
    extra_claims={"permissions": list(user.get_all_permissions())}
)
```

### get_current_user

Dependency for getting the authenticated user.

```python
from django_bolt import Depends
from django_bolt.auth import get_current_user

@api.get("/me")
async def me(user=Depends(get_current_user)):
    return {"username": user.username}
```

## Revocation stores

When passed as `JWTAuthentication(revocation_store=store)`, the framework
checks every authenticated request against the store and rejects revoked
tokens with `401`. You don't call `is_revoked()` from your handlers.

All `revoke()` calls accept an `exp` keyword: pass the token's own `exp`
claim so the entry expires exactly when the token would have. Call
sites without `exp` fall back to `default_ttl` (set per instance) or to
the module-level `_DEFAULT_TTL_SECONDS` (7 days).

### InMemoryRevocation

In-memory token revocation (development only — single process, no
persistence).

```python
from django_bolt.auth import InMemoryRevocation

store = InMemoryRevocation()
await store.revoke("token-jti", exp=1234567890)
await store.is_revoked("token-jti")  # True
```

### DjangoCacheRevocation

Django cache-based revocation.

```python
from django_bolt.auth import DjangoCacheRevocation

store = DjangoCacheRevocation(
    cache_alias="default",
    key_prefix="revoked:",
    default_ttl=86400 * 7,  # fallback when revoke() is called without exp
)
```

### DjangoORMRevocation

Database-backed revocation.

```python
from django_bolt.auth import DjangoORMRevocation

store = DjangoORMRevocation(
    model="myapp.RevokedToken",  # 'app_label.ModelName' — exactly two parts
    default_ttl=86400 * 7,
)
```

Requires a model with `jti` (unique, indexed) and `expires_at`
(indexed) fields, plus a periodic cleanup task that deletes rows where
`expires_at < now()`.

## Authentication context

After authentication, `request.context` contains:

| Key            | Type        | Description                     |
| -------------- | ----------- | ------------------------------- |
| `user_id`      | `str`       | User identifier                 |
| `is_staff`     | `bool`      | Staff status                    |
| `is_superuser` | `bool`      | Superuser status                |
| `auth_backend` | `str`       | Backend name (`jwt`, `api_key`) |
| `permissions`  | `list[str]` | User permissions                |
| `auth_claims`  | `dict`      | JWT claims (JWT only)           |

```python
@api.get("/info")
async def info(request):
    return {
        "user_id": request.user.id,
        "backend": request.context.get("auth_backend"),
    }
```

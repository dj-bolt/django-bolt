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
    oidc_issuer=None,         # OIDC discovery issuer
    jwks_refresh_interval=300,
)
```

#### Parameters

| Parameter          | Type            | Default           | Description             |
| ------------------ | --------------- | ----------------- | ----------------------- |
| `secret`           | `str`           | Django SECRET_KEY | HMAC secret (also accepts a PEM public key for compatibility) |
| `public_key`       | `str`           | `None`            | PEM public key for asymmetric algorithms (preferred over `secret`) |
| `algorithms`       | `list[str]`     | `["HS256"]`       | Allowed JWT algorithms (token `alg` must be in this list) |
| `header`           | `str`           | `"authorization"` | Header containing token |
| `cookie`           | `str`           | `None`            | Cookie containing token (replaces `header` when set) |
| `audience`         | `str`           | `None`            | Required `aud` claim    |
| `issuer`           | `str`           | `None`            | Required `iss` claim    |
| `leeway`           | `int`           | `60`              | Clock-skew tolerance (seconds) for `exp`/`nbf` |
| `token_type`       | `str`           | `None`            | Required `typ` claim (e.g. `"refresh"` for a rotation endpoint); access routes reject `typ:"refresh"` |
| `csrf`             | `bool`          | `True`            | For cookie tokens, enforce a cross-site origin check on unsafe methods that carry the auth cookie |
| `jwks_url`         | `str`           | `None`            | HTTPS JWKS endpoint; requires `issuer` and `audience`, refreshed at runtime |
| `jwks`             | `dict \| str`   | `None`            | JWKS document supplied directly (alternative to `jwks_url`) |
| `oidc_issuer`      | `str`           | `None`            | HTTPS issuer for OIDC discovery; requires `audience` |
| `jwks_refresh_interval` | `int`       | `300`             | Periodic remote JWKS refresh interval in seconds |
| `revocation_store` | RevocationStore | `None`            | Token revocation store  |

#### Supported algorithms

- HMAC: `HS256`, `HS384`, `HS512` — `secret` is the shared secret
- RSA: `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512` — `secret` is a PEM-encoded RSA public key
- ECDSA: `ES256`, `ES384` — `secret` is a PEM-encoded EC public key
- EdDSA: `EdDSA` (Ed25519) — `secret` is a PEM-encoded Ed25519 public key

All algorithms configured on a single backend must use the same kind of
key; you cannot mix `HS256` with `RS256`. Configuration errors — an
unknown algorithm name, algorithms from different key families, or a key
that is not valid PEM — stop the server at startup with a descriptive
error, rather than silently rejecting every token at runtime.

To verify tokens issued by an external identity provider such as Clerk
or Auth0, pass the provider's PEM public key:

```python
JWTAuthentication(
    public_key=CLERK_PEM_PUBLIC_KEY,   # from the provider's dashboard
    algorithms=["RS256"],
    issuer="https://your-app.clerk.accounts.dev",
)
```

See [Verifying tokens from an identity
provider](../topics/authentication.md#verifying-tokens-from-an-identity-provider)
for JWKS configuration.

#### Validation behavior

Tokens are validated in Rust, using verification keys built once at
server startup:

- The token's `alg` header must appear in `algorithms`. A token naming
  any other algorithm is rejected before signature verification, which
  prevents algorithm confusion attacks. Header extension parameters of
  any JSON type are accepted, as RFC 7515 permits; providers such as
  Clerk and Auth0 include non-string parameters.
- The `exp` claim is required. `exp` and `nbf` are checked with `leeway`
  seconds of clock-skew tolerance (60 by default).
- The `aud` claim may be a single string or an array. It is validated
  only when `audience` is configured; a token is not rejected merely for
  carrying an `aud` claim when no audience was configured.
- When `issuer` is configured, tokens without an `iss` claim are
  rejected.
- When the token is read from a cookie, state-changing requests that
  carry the cookie must also pass a cross-site origin check. See
  [Cross-site request forgery
  protection](../topics/authentication.md#cross-site-request-forgery-protection).

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

### create_token_pair

Mint an access + refresh token pair. See [Access and refresh
tokens](../topics/authentication.md#access-and-refresh-tokens) for usage.

```python
from django_bolt.auth import create_token_pair

pair = create_token_pair(user, method="pwd", kid="signing-key-2026-07")
pair.access_token     # JWT with typ "access"
pair.refresh_token    # JWT with typ "refresh"
pair.access_claims    # decoded claims of the access token
pair.refresh_claims   # decoded claims of the refresh token
```

#### Parameters

| Parameter     | Type                 | Default           | Description                                    |
| ------------- | -------------------- | ----------------- | ---------------------------------------------- |
| `user`        | User \| `str` \| `int` | required        | Django user instance, or a bare user id        |
| `secret`      | `str`                | Django SECRET_KEY | Signing key                                    |
| `algorithm`   | `str`                | `"HS256"`         | JWT signing algorithm                          |
| `kid`         | `str`                | `None`            | Optional signing-key identifier added to both JWT headers |
| `access_ttl`  | `int`                | `900`             | Access token lifetime in seconds               |
| `refresh_ttl` | `int`                | `604800`          | Refresh token lifetime in seconds              |
| `claims`      | `dict`               | `None`            | Extra claims copied into both tokens           |
| `method`      | `str`                | `None`            | Authentication method, recorded as `amr` (RFC 8176) |
| `version`     | `int`                | `0`               | The user's current token version, embedded as `ver` |
| `oat`         | `int`                | now               | Origin auth time; set by rotation to carry the original value |

Both tokens carry `sub`, `iat`, `oat`, `ver`, `typ`, and `exp`; the
refresh token additionally carries `jti` and `fam`. These lifecycle
claims are reserved — passing one in `claims` raises `ValueError`.

### rotate_refresh_token

Exchange a validated refresh token for a new pair. This function is a
coroutine; the claims you pass are the already-verified claims from
`request["context"]["auth_claims"]`.

```python
from django_bolt.auth import rotate_refresh_token

pair = await rotate_refresh_token(claims, store=store)
```

#### Parameters

| Parameter              | Type            | Default           | Description                                          |
| ---------------------- | --------------- | ----------------- | ---------------------------------------------------- |
| `refresh_claims`       | `dict`          | required          | Verified claims of the presented refresh token       |
| `store`                | RevocationStore | required          | Store used for revocation and version checks         |
| `secret`               | `str`           | Django SECRET_KEY | Signing key for the new tokens                       |
| `algorithm`            | `str`           | `"HS256"`         | JWT signing algorithm                                |
| `kid`                  | `str`           | `None`            | Optional signing-key identifier added to newly issued JWT headers |
| `access_ttl`           | `int`           | `900`             | New access token lifetime in seconds                 |
| `refresh_ttl`          | `int`           | `604800`          | New refresh token lifetime in seconds                |
| `rotate`               | `bool`          | `True`            | Issue a new refresh token and revoke the old one; `False` issues an access token only |
| `max_session_lifetime` | `int`           | `None`            | Maximum seconds since the original authentication (`oat`) |
| `leeway`               | `int`           | `60`              | Clock-skew tolerance; must match the validating JWT backend |
| `claims`               | `dict`          | `None`            | Extra claims for the new tokens                      |

Raises `TokenRotationError` when the token has no `jti`, has been
revoked or reused, belongs to a revoked family, carries a stale `ver`,
or exceeds `max_session_lifetime`. Return a generic `401` to the client
in that case.

### set_token_cookies

Attach a token pair to a response as `HttpOnly`, `Secure`,
`SameSite=Lax` cookies. Returns the response for chaining.

```python
from django_bolt.auth import set_token_cookies

return set_token_cookies(response, pair, refresh_path="/auth/refresh")
```

#### Parameters

| Parameter        | Type   | Default           | Description                                    |
| ---------------- | ------ | ----------------- | ---------------------------------------------- |
| `response`       | object | required          | Any response exposing `set_cookie()`           |
| `pair`           | TokenPair | required       | The pair to attach                             |
| `access_cookie`  | `str`  | `"access_token"`  | Access cookie name                             |
| `refresh_cookie` | `str`  | `"refresh_token"` | Refresh cookie name                            |
| `refresh_path`   | `str`  | `"/"`             | Path the refresh cookie is scoped to; set this to your rotation endpoint |
| `secure`         | `bool` | `True`            | Send cookies over HTTPS only                   |
| `samesite`       | `str`  | `"Lax"`           | `SameSite` attribute                           |
| `domain`         | `str`  | `None`            | Cookie domain                                  |

Each cookie's `max_age` is derived from its token's own `exp` claim.

## Revocation stores

When passed as `JWTAuthentication(revocation_store=store)`, the framework
checks every authenticated request against the store and rejects revoked
tokens with `401`. You don't call `is_revoked()` from your handlers.

All `revoke()` calls accept an `exp` keyword: pass the token's own `exp`
claim so the entry expires exactly when the token would have. Call
sites without `exp` fall back to `default_ttl` (set per instance) or to
the module-level `_DEFAULT_TTL_SECONDS` (7 days).

### Store methods

All methods are coroutines.

| Method                          | Description                                                    |
| ------------------------------- | -------------------------------------------------------------- |
| `revoke(jti, *, exp=None)`      | Revoke a single token by its `jti` claim                       |
| `is_revoked(jti)`               | Whether a token has been revoked                               |
| `consume(jti, *, exp=None)`     | Atomically consume a refresh token; `True` only for the first caller |
| `get_user_version(user_id)`     | The user's current token version (`0` if never bumped)         |
| `bump_user_version(user_id)`    | Increment the version, invalidating earlier refresh tokens at rotation |
| `revoke_family(fam, *, exp=None)` | Revoke an entire refresh-token rotation family               |
| `is_family_revoked(fam)`        | Whether a rotation family has been revoked                     |

The version and family methods support the [access and refresh token
lifecycle](../topics/authentication.md#access-and-refresh-tokens). They
are implemented by `InMemoryRevocation` and `DjangoCacheRevocation`.
`DjangoORMRevocation` supports atomic consumption and family revocation,
but not user-version methods.

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

Use Redis or Memcached when refresh rotation or user-version bumps must
work across processes. Those operations require atomic cache `add` and
`incr`; file-based, database, dummy, and other non-atomic caches are
rejected. Ensure the cache does
not evict security entries before their configured TTL.

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

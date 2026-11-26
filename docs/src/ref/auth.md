---
description: Reference for django_bolt.auth. Backends, guards, the request context, token helpers, JWTAuthViews, revocation stores and settings.
---

# Authentication reference

This page lists the public API of `django_bolt.auth`. For a task-based introduction, see [Authentication](../topics/authentication.md) and [Permissions](../topics/permissions.md).

Import each name from `django_bolt.auth`. `JWTAuthentication`, `APIKeyAuthentication`, `SessionAuthentication`, the three guards, `CurrentUser`, `OptionalCurrentUser`, `Token`, `create_jwt_for_user` and `get_current_user` are also in `django_bolt`.

## Backends

A backend reads a credential. Pass a list of backend instances to `auth=` of a route, to `Router(auth=...)` or to `BOLT_AUTHENTICATION_CLASSES`.
Bolt tries the backends in list order. The first backend that accepts the credential wins. A `SessionAuthentication` is always tried last.
Bolt reads the configuration of each backend one time, when it registers the route.

### JWTAuthentication

<!-- fragment -->
```python
JWTAuthentication(
    secret: str | None = None,
    algorithms: list[str] | None = None,
    header: str = "authorization",
    cookie: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
    revoked_token_handler: Callable | None = None,
    revocation_store: RevocationStore | None = None,
    require_jti: bool = False,
    public_key: str | None = None,
    leeway: int = 60,
    token_type: str | None = None,
    csrf: bool = True,
    jwks_url: str | None = None,
    jwks: dict | str | None = None,
    oidc_issuer: str | None = None,
    jwks_refresh_interval: int = 300,
)
```

Verifies a JSON Web Token in Rust.

| Parameter | Default | Description |
| --- | --- | --- |
| `secret` | `None` | The HMAC key. `None` uses `settings.SECRET_KEY`, read when Bolt registers the route. |
| `algorithms` | `None` | The accepted `alg` values. `None` means `["HS256"]`. All values must use one kind of key. |
| `header` | `"authorization"` | The header that holds the token. Bolt removes a `Bearer ` scheme, in any case. A token with no scheme also works. |
| `cookie` | `None` | Read the token from this cookie only. The header is then not read. |
| `audience` | `None` | Require this value in the `aud` claim, a string or a list. `None` does not check `aud`. |
| `issuer` | `None` | Require this exact `iss` claim. `None` does not check `iss`. |
| `revoked_token_handler` | `None` | An async callable `(jti)` or `(jti, claims)`. When it returns `True`, the request gets 401 `{"detail": "Token has been revoked"}`. |
| `revocation_store` | `None` | A [revocation store](#revocation). Bolt checks the `jti` of each token in it. |
| `require_jti` | `False` | Reject a token with no `jti` claim. A handler or a store sets it to `True`. |
| `public_key` | `None` | A PEM public key for an `RS*`, `PS*`, `ES*` or `EdDSA` algorithm. |
| `leeway` | `60` | Seconds of clock skew allowed on `exp` and `nbf`. |
| `token_type` | `None` | Require this `typ` claim, for example `"refresh"`. See below. |
| `csrf` | `True` | With `cookie`, check the origin of each unsafe request. See below. |
| `jwks_url` | `None` | An HTTPS URL of a JSON Web Key Set. Needs `issuer` and `audience`. |
| `jwks` | `None` | A JSON Web Key Set, as a dict or a JSON string. |
| `oidc_issuer` | `None` | An HTTPS issuer URL. Bolt reads `/.well-known/openid-configuration` in the constructor. Needs `audience`. |
| `jwks_refresh_interval` | `300` | Seconds between fetches of a `jwks_url` key set. |

Supported algorithms: `HS256`, `HS384`, `HS512`, `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384`, `EdDSA`.

Checks on each token:

- The `alg` header is in `algorithms`, and the signature is valid. A `jwks` key is selected by the `kid` header.
- `exp` is present and not more than `leeway` seconds in the past. `nbf`, when present, is not more than `leeway` seconds in the future.
- `aud` and `iss`, when you configure them.
- With `token_type=None`: the `typ`, `token_type` and `token_use` claims are not `refresh`, `offline` or `id`, in any case.
- With `token_type="refresh"`: `typ` is `"refresh"`, and `token_type` and `token_use` do not name another type.
- With `require_jti`: `jti` is present and not empty.
- With `cookie`: the request sends the cookie name one time only.

The CSRF check of a cookie backend applies when that backend accepted the request and the method is not `GET`, `HEAD`, `OPTIONS` or `TRACE`.
A `Sec-Fetch-Site` header must be `same-origin` or `none`. With no `Sec-Fetch-Site`, the scheme and host of `Origin`, or else of `Referer`, must equal those of the request.
A request with none of these headers fails. A failure gets 403 `{"detail": "Permission denied"}`.
For the scheme, Bolt reads `X-Forwarded-Proto` from any peer. The check passes only one origin, so the header cannot make it weaker.

The key set of a `jwks_url` is fetched when Bolt registers the route, and again each `jwks_refresh_interval` seconds on a background thread.
A token with an unknown `kid` starts a fetch at once and waits for it. After that, an unknown `kid` starts a fetch after 30 seconds or more. A failed fetch keeps the last keys.

The `WWW-Authenticate` scheme of the backend is `Bearer` with the `authorization` header and no `cookie`. Otherwise it is `ApiKey`.

**Errors.** The constructor raises `ImproperlyConfigured` for:

- both `secret` and `public_key`, or a key with `jwks_url` or `jwks`, or both `jwks_url` and `jwks`;
- `oidc_issuer` with `issuer`, `jwks_url` or `jwks`; `oidc_issuer` with no `audience`; a discovery document with another issuer;
- a `jwks_url` or `oidc_issuer` that is not an absolute HTTPS URL; `jwks_url` with no `issuer` or no `audience`;
- an empty `secret`, `header`, `cookie`, `audience`, `issuer` or `token_type`, or an empty `algorithms` list;
- `leeway < 0`, `jwks_refresh_interval <= 0`, or a `revocation_store` whose `leeway` is smaller than `leeway`.

Registration raises `ValueError` for an unknown algorithm, algorithms that mix key kinds, or a key that is not valid PEM.

**User loading.** `await request.auser()` and `request.user` load the user with the primary key in `sub`. An inactive user or a missing row gives no user.
Override `async get_user(self, user_id: str, auth_context: dict)` or `get_user_sync(self, user_id: str)` in a subclass to change the query.
With only an async `get_user`, a sync read of `request.user` raises `RuntimeError`.

### APIKeyAuthentication

<!-- fragment -->
```python
APIKeyAuthentication(
    api_keys: set[str] | None = None,
    header: str = "x-api-key",
    key_permissions: dict[str, set[str]] | None = None,
)
```

Accepts a key from a fixed set.

| Parameter | Default | Description |
| --- | --- | --- |
| `api_keys` | `None` | A set, list or tuple of valid keys. With no keys, the backend accepts no request. |
| `header` | `"x-api-key"` | The header that holds the key. Its value is the key, `Bearer <key>` or `ApiKey <key>`. |
| `key_permissions` | `None` | A dict from a key to its permissions. `Requires("permissions", ...)` reads them. |

Bolt compares keys in constant time. The context of an accepted request has `"user_id": "apikey:<key>"` and `"auth_backend": "api_key"`.
The backend loads no user: `request.user` and `await request.auser()` give `AnonymousUser`.
Its `WWW-Authenticate` scheme is `ApiKey`.

**Errors.** `ImproperlyConfigured` for `api_keys` that is a string or holds a value that is not a string, an empty key, a key that starts with `Bearer ` or `ApiKey ` in any case, an empty `header`, a `key_permissions` that is not a dict, or a `key_permissions` key that is not in `api_keys`.

### SessionAuthentication

<!-- fragment -->
```python
SessionAuthentication(*, login_url: str | None = None, csrf: bool = True)
```

Authenticates with Django's session cookie, `settings.SESSION_COOKIE_NAME`. Works on HTTP routes only.

| Parameter | Default | Description |
| --- | --- | --- |
| `login_url` | `None` | Redirect a request with no logged-in user to this URL, with Django's `redirect_to_login()`. With `None`, the request gets 401. |
| `csrf` | `True` | Do the CSRF check of a cookie backend on each unsafe request to the route, also with no cookie. |

Bolt adds Django's `SessionMiddleware` and `AuthenticationMiddleware` to each route with this backend, unless the middleware of the route already has both.
With guards that can reject and no `login_url`, Rust answers 401 to a request with no session cookie. Python checks the guards after Django loads the user.
A guard can read `is_staff`, `is_superuser` and `permissions` (from `get_all_permissions()`). `Requires` on another claim rejects with 403.
Its `WWW-Authenticate` scheme is `ApiKey`.

**Errors.** At registration, `ImproperlyConfigured` for: the backend on a WebSocket route or an MCP mount; route middleware with only one of the two Django classes;
`Requires(claim, none_of=...)` on a claim that a session user does not have; a guard that reads a field that the user model does not have.
A session backend from `BOLT_AUTHENTICATION_CLASSES` is left out on WebSocket routes and MCP mounts.

### BaseAuthentication

The base class of the backends. Rust does the verification, so a subclass cannot add a new kind of credential.
Subclass a backend above to change one of these members:

| Member | Description |
| --- | --- |
| `async get_user(self, user_id, auth_context)` | Return the user of `await request.auser()`, or `None`. |
| `challenge_scheme` | A property. The scheme in the `WWW-Authenticate` header of a 401. |
| `scheme_name` | A property: `"jwt"`, `"api_key"` or `"session"`. |
| `to_metadata()` | The configuration that Rust reads. |

## Guards

A guard decides if a request can continue. Pass a list to `guards=` of a route, to `Router(guards=...)` or to `BOLT_DEFAULT_PERMISSION_CLASSES`.
Bolt checks the guards in order and stops at the first result. A guard can be an instance or a class with no arguments, such as `IsAuthenticated`.
Another object raises `ImproperlyConfigured` at registration.

### AllowAny

<!-- fragment -->
```python
AllowAny()
```

Allows the request and stops the check of the guards after it.
Without `BOLT_AUTHENTICATION_CLASSES`, a route with no `auth` and `AllowAny()` as its first guard gets no backend.

### IsAuthenticated

<!-- fragment -->
```python
IsAuthenticated()
```

Allows a request when a backend accepted a credential with a user ID. Otherwise answers 401 `{"detail": "Authentication required"}`.

### Requires

<!-- fragment -->
```python
Requires(
    claim: str,
    *values: str | int | float | bool,
    all_of: list | tuple | None = None,
    none_of: list | tuple | None = None,
    message: str | None = None,
)
```

Compares one claim of the credential with the values.

| Parameter | Description |
| --- | --- |
| `claim` | The name of the claim. `"permissions"` reads the permission set of any backend. |
| `values` | The claim must match one of them. With no values and no `all_of` or `none_of`, the claim must be present, not `null`, and not `false`. |
| `all_of` | The claim, a list, must contain each value. |
| `none_of` | The claim must match no value. An absent claim passes. |
| `message` | The `detail` of the 403. The default is `"Permission denied"`. Not used for a 401. |

A scalar claim matches an equal value. A list claim matches a value that it contains.
A request with no credential gets 401 for each `Requires`, also for a `none_of` one. A credential that does not match gets 403.

**Errors.** `ImproperlyConfigured` for an empty or non-string `claim`, more than one of `values`, `all_of` and `none_of`, an `all_of` or `none_of` that is a string or is empty, a value that is not a `str`, `int`, `float` or `bool`, or an empty `message`.

## Request context

`request.context` is a dict, or `None` when no backend accepted a credential.

| Key | Type | Description |
| --- | --- | --- |
| `user_id` | `str` | The `sub` claim, or `"apikey:<key>"`. Absent for a session. |
| `is_staff` | `bool` | The `is_staff` claim, or `False` |
| `is_superuser` | `bool` | The `is_superuser` claim, or `False` |
| `auth_backend` | `str` | `"jwt"`, `"api_key"` or `"session"` |
| `auth_backend_index` | `int` | The position of the backend in the list of the route |
| `permissions` | `list[str]` | Present only when the credential has permissions |
| `auth_claims` | `dict` | JWT only. The claims, without `permissions`. A custom claim that is a JSON array or object has the value `None`. |

### get_auth_context

<!-- fragment -->
```python
get_auth_context(request: Request) -> dict
```

Returns `request.context`, or `{}`.

### extract_user_id_from_context

<!-- fragment -->
```python
extract_user_id_from_context(request: Request) -> str | None
```

Returns `request.context["user_id"]`, or `None`.

### AuthContext

A dataclass with the fields `user_id`, `is_staff`, `is_superuser`, `backend`, `claims` and `permissions`. Bolt does not use it at runtime. `request.context` is a dict.

## Current user

| Name | Type | Description |
| --- | --- | --- |
| `CurrentUser` | Annotated type | A handler parameter. Gives the user, or answers 401 `{"detail": "Authentication required"}`. |
| `OptionalCurrentUser` | Annotated type | A handler parameter. Gives the user, or `None`. |
| `get_current_user(request)` | async dependency | Returns the user, or `None`. |
| `get_current_user_sync(request)` | sync dependency | The sync form, for a sync handler |
| `require_current_user(request)` | async dependency | Returns the user, or raises `Unauthorized` |
| `require_current_user_sync(request)` | sync dependency | The sync form, for a sync handler |

A sync handler uses the sync form of `CurrentUser` and `OptionalCurrentUser`. For the type of your user model, write `Annotated[User, Depends(require_current_user)]`.
"No user" includes an inactive user and a user ID with no row.

## Tokens

### create_jwt_for_user

<!-- fragment -->
```python
create_jwt_for_user(
    user,
    secret: str | None = None,
    algorithm: str = "HS256",
    expires_in: int = 3600,
    extra_claims: dict | None = None,
) -> str
```

Returns a signed token for a Django user. `secret=None` uses `settings.SECRET_KEY`.
The claims are `sub` (the user `id` as a string), `iat`, `exp`, `is_staff`, `is_superuser` and `username`, plus `email`, `first_name` and `last_name` when they are not empty, plus `extra_claims`.
There is no `jti`.

**Errors.** `ValueError` when `extra_claims` has `sub`, `exp`, `iat`, `is_staff` or `is_superuser`.

### create_token_pair

<!-- fragment -->
```python
create_token_pair(
    user,
    *,
    secret: str | None = None,
    algorithm: str = "HS256",
    kid: str | None = None,
    access_ttl: int = 900,
    refresh_ttl: int = 604800,
    claims: dict | None = None,
    method: str | None = None,
    version: int = 0,
    oat: int | None = None,
) -> TokenPair
```

Returns a new access token and refresh token.

| Parameter | Description |
| --- | --- |
| `user` | A user, or a user ID. `sub` is `str(user.pk)`, or `str(user)`. |
| `secret` | The signing key. `None` uses `SECRET_KEY`. A PEM private key for an asymmetric algorithm. |
| `kid` | Added to the header of both tokens |
| `access_ttl`, `refresh_ttl` | The lifetimes in seconds: 15 minutes and 7 days |
| `claims` | Extra claims for both tokens |
| `method` | Written as the `amr` claim, a list, for example `["pwd"]` |
| `version` | The `ver` claim. Pass `await store.get_user_version(user_id)`. |
| `oat` | The origin auth time. `None` uses the current time. |

Both tokens have `sub`, `iat`, `oat`, `ver`, `exp`, a new `jti`, and `typ` (`"access"` or `"refresh"`). The refresh token also has `fam`, a new family ID.

**Errors.** `ValueError` when `claims` has `sub`, `iat`, `exp`, `typ`, `jti`, `fam`, `oat` or `ver`.

### TokenPair

A dataclass with `access_token: str`, `refresh_token: str`, `access_claims: dict` and `refresh_claims: dict`.
`refresh_token` is `""` after a rotation with `rotate=False`.

### rotate_refresh_token

<!-- fragment -->
```python
async rotate_refresh_token(
    refresh_claims: dict,
    *,
    store: RevocationStore,
    secret: str | None = None,
    algorithm: str = "HS256",
    kid: str | None = None,
    access_ttl: int = 900,
    refresh_ttl: int = 604800,
    rotate: bool = True,
    max_session_lifetime: int | None = None,
    leeway: int = 60,
    claims: dict | None = None,
) -> TokenPair
```

Exchanges verified refresh claims, `request.context["auth_claims"]`, for new tokens.

| Parameter | Description |
| --- | --- |
| `store` | A store with `consume`, `revoke_family`, `is_family_revoked` and `get_user_version` |
| `rotate` | `True` marks the refresh token as used and gives a new pair. `False` gives a new access token only, and keeps the refresh token valid. |
| `max_session_lifetime` | Seconds after `oat` when the session ends. The new access token does not live past that time. |
| `leeway` | The `leeway` of the `JWTAuthentication` that verified the token |
| `claims` | Extra claims for the new tokens. Custom claims of the old token are not copied. `amr` and `oat` are copied. |

The checks: `typ` is `"refresh"`; `jti`, `sub` and (with `rotate=True`) `fam` are present; the family is not revoked; `ver` equals the current user version; the session is younger than `max_session_lifetime`.
A used token sent again revokes its family.

**Errors.** `TokenRotationError` for each failed check, and for a store that cannot do the operation. `ValueError` for a negative `leeway` or `max_session_lifetime`, and for a reserved claim in `claims`.

### TokenRotationError

An `Exception`. Its message tells why the rotation failed. Answer a uniform 401 and do not send the message.

### set_token_cookies

<!-- fragment -->
```python
set_token_cookies(
    response,
    pair: TokenPair,
    *,
    access_cookie: str = "access_token",
    refresh_cookie: str = "refresh_token",
    refresh_path: str = "/",
    secure: bool = True,
    samesite: str = "Lax",
    domain: str | None = None,
) -> response
```

Sets the tokens as `HttpOnly` cookies and returns `response`. The access cookie has the path `/`. The refresh cookie has `refresh_path`; set it to the path of your refresh route.
`max_age` of each cookie ends at the `exp` of its token. No refresh cookie is set when `pair.refresh_token` is empty.
`response` is an object with `set_cookie()`, such as `JSON` or Django's `HttpResponse`.

### Token

<!-- fragment -->
```python
Token(
    exp: datetime,
    sub: str,
    iat: datetime = now,
    iss: str | None = None,
    aud: str | None = None,
    jti: str | None = None,
    nbf: datetime | None = None,
    is_staff: bool | None = None,
    is_superuser: bool | None = None,
    is_admin: bool | None = None,
    permissions: list[str] | None = None,
    extras: dict = {},
)
```

A dataclass for a token with typed claims. The datetimes become UTC with no microseconds.

| Method | Description |
| --- | --- |
| `encode(secret, algorithm="HS256", headers=None) -> str` | Signs the token with PyJWT |
| `Token.decode(encoded_token, secret, algorithm="HS256", audience=None, issuer=None, verify_exp=True, verify_nbf=True) -> Token` | Verifies and reads a token in Python |
| `Token.create(sub, expires_delta=timedelta(hours=1), issuer=None, audience=None, is_staff=False, is_admin=False, permissions=None, **extra_claims) -> Token` | Builds a token. `is_admin=True` also sets `is_staff`. |
| `to_dict() -> dict` | The claims, with times in seconds since the epoch |

**Errors.** `ValueError` for an empty `sub`, an `exp` in the past, an `iat` in the future, a field name in `extras`, or a failed `encode` or `decode`. `TypeError` for an `iat` or `nbf` that is not a `datetime`.

## JWTAuthViews

<!-- fragment -->
```python
JWTAuthViews(
    *,
    store: RevocationStore,
    secret: str | None = None,
    algorithm: str = "HS256",
    public_key: str | None = None,
    kid: str | None = None,
    prefix: str = "/auth",
    access_cookie: str = "access_token",
    refresh_cookie: str = "refresh_token",
    secure_cookies: bool = True,
    credential_validator: Callable | None = None,
)
```

Login, refresh and logout routes with cookies. Call `register(api)` to add them to a `BoltAPI`.

| Parameter | Description |
| --- | --- |
| `store` | The revocation store of the refresh tokens |
| `secret` | The signing key. `None` uses `SECRET_KEY`. With an asymmetric `algorithm`, the PEM private key. |
| `public_key` | With an asymmetric `algorithm`, the PEM public key that verifies the refresh token |
| `prefix` | The path prefix of the routes, and the path of the refresh cookie |
| `secure_cookies` | The `Secure` flag of both cookies |
| `credential_validator` | An async callable `(request, credentials: LoginCredentials) -> user \| None`. The default calls Django's `aauthenticate()`. |

| Route | Body | Response |
| --- | --- | --- |
| `POST {prefix}/login` | `LoginCredentials` | `{"ok": true}` and both cookies; 401 `{"detail": "Invalid credentials"}` |
| `POST {prefix}/refresh` | none | `{"ok": true}` and new cookies; 401 `{"detail": "Invalid token"}` |
| `POST {prefix}/logout` | none | `{"ok": true}`; revokes the family; deletes the cookies |
| `POST {prefix}/logout-all` | none | `{"ok": true}`; adds 1 to the user version; deletes the cookies |

The access token lives 15 minutes and the refresh token 7 days. The cookies are `HttpOnly` and `SameSite=Lax`.
The refresh, logout and logout-all routes read the refresh cookie with `token_type="refresh"` and do the CSRF check of a cookie backend.
With the default validator, login and refresh refuse an inactive user.
The logout routes accept only a refresh token that the refresh route also accepts.
The routes have the OpenAPI tag `auth`.

**Errors.** `ImproperlyConfigured` for `public_key` with an `HS*` algorithm, or for an asymmetric algorithm with no `secret` or no `public_key`.

### LoginCredentials

A `msgspec.Struct` with `username: str` and `password: str`. The body of the login route.

## Revocation

A revocation store keeps the IDs of revoked tokens. All methods are async.

### RevocationStore

The base class. Subclass it for another storage.

| Method | Description |
| --- | --- |
| `is_revoked(jti) -> bool` | Required. Whether the token is revoked. |
| `revoke(jti, *, exp=None)` | Required. Revoke a token. Pass its `exp`, so the entry lives until `exp` plus the leeway. |
| `consume(jti, *, exp=None) -> bool` | Mark a refresh token as used, atomically. `True` only for the first call. |
| `revoke_family(fam, *, exp=None)` | Revoke a refresh token family |
| `is_family_revoked(fam) -> bool` | Whether a family is revoked |
| `get_user_version(user_id) -> int` | The token version of a user, 0 by default |
| `bump_user_version(user_id) -> int` | Add 1 to the token version, and return it |
| `revoke_all(user_id)` | No store of Bolt implements it. Use `bump_user_version`. |

An optional method that a store does not have raises `NotImplementedError`.

`DjangoCacheRevocation` and `DjangoORMRevocation` keep families and user versions in the same key space as tokens.
Thus their `revoke`, `consume` and `is_revoked` raise `ReservedJtiError` for a `jti` with a reserved prefix.
`ReservedJtiError` is a subclass of `ValueError`.
For `DjangoCacheRevocation`, the reserved prefixes are `fam:` and `ver:`.
For `DjangoORMRevocation`, they are `family:` and `version:`.
A `jti` from `create_token_pair` is a UUID, so it does not have these prefixes.
`JWTAuthentication(revocation_store=store)` and `JWTAuthViews` count a token as revoked when `is_revoked` raises `ReservedJtiError`.
Thus such a token gets a 401.
The two Django stores also raise `ReservedJtiError` for a key that they cannot keep.
`DjangoORMRevocation` refuses a key longer than the `max_length` of the `jti` field of its model, and a key with a NUL character.
`DjangoCacheRevocation` refuses a key that the cache backend refuses with `InvalidCacheKey`.
For example, a memcached backend refuses a key with a space, or a key longer than 250 characters.
These checks apply to the `jti`, and to the keys of families and user versions.
`JWTAuthViews` gives a 401 for a refresh token whose `fam` or `sub` the store cannot keep, and `rotate_refresh_token` raises `TokenRotationError`.
A custom store can raise `ReservedJtiError` for a `jti` that it cannot keep.
Another exception from `is_revoked` gives a 500.
`InMemoryRevocation` keeps tokens, families and user versions apart, so it accepts each `jti`.
The two Django stores give `False` from `consume` for a token after `exp + leeway`.
`InMemoryRevocation` does not read `exp`.

### InMemoryRevocation

<!-- fragment -->
```python
InMemoryRevocation()
```

Keeps entries in the memory of one process until it stops. It has all methods. `clear()` empties it. Use it for tests and one process.

### DjangoCacheRevocation

<!-- fragment -->
```python
DjangoCacheRevocation(
    cache_alias: str = "default",
    key_prefix: str = "revoked:",
    *,
    default_ttl: int = 604800,
    leeway: int = 60,
)
```

Keeps entries in a Django cache. An entry lives until `exp + leeway`, or `default_ttl` seconds when `revoke()` gets no `exp`. A user version never expires.
A database cache (`DatabaseCache` or a subclass) runs a query for each call. Thus the store uses the async cache API for it, and each call runs in a thread.
The store calls each other cache directly on the event loop.
`consume()` and `bump_user_version()` raise `NotImplementedError` for the file, dummy and database cache backends, and for each backend with no atomic `incr`.
`leeway < 0` raises `ImproperlyConfigured`.

### DjangoORMRevocation

<!-- fragment -->
```python
DjangoORMRevocation(
    model: str = "bolt_revocation.RevokedToken",
    *,
    default_ttl: int = 604800,
    leeway: int = 60,
)
```

Keeps entries in a database table. Add `"django_bolt.auth.revocation_db"` to `INSTALLED_APPS` and run `migrate`.
`model` is an `"app_label.ModelName"` with the fields `jti` (unique), `revoked_at`, `expires_at` and `version`. Another form raises `ValueError` on first use.
A family is a row with the `jti` `family:<fam>`. A user version is a row with the `jti` `version:<user_id>` that never expires.
Thus a token `jti` cannot start with `family:` or `version:`.
The store does not delete rows. Delete rows with an `expires_at` in the past from a periodic task:

<!-- fragment -->
```python
await RevokedToken.objects.filter(expires_at__lt=timezone.now()).adelete()
```

`leeway < 0` raises `ImproperlyConfigured`. `RevokedToken` is in `django_bolt.auth.revocation_db.models`.

### create_revocation_handler

<!-- fragment -->
```python
create_revocation_handler(store: RevocationStore) -> Callable
```

Returns an async `handler(jti)` that calls `store.is_revoked(jti)`. `JWTAuthentication(revocation_store=store)` does this for you.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `BOLT_AUTHENTICATION_CLASSES` | not set | The backends of a route with no `auth`. When not set, a route whose first guard is not `AllowAny` gets `[JWTAuthentication()]`, and other routes get none. `[]` means none. |
| `BOLT_DEFAULT_PERMISSION_CLASSES` | `[AllowAny()]` | The guards of a route with no `guards` |

`get_default_authentication_classes(*, needs_user=False)` and `get_default_permission_classes()` return these values.

## User loading

Bolt calls `register_auth_backend(backend_name, backend_instance)` at startup. `get_registered_backend(backend_name)` returns a registered backend.
`await load_user(user_id, backend_name, auth_context=None)` loads a user with the loader of a registered backend. You do not need these functions in a handler. Use `CurrentUser` or `await request.auser()`.

## See also

- [Authentication](../topics/authentication.md)
- [Permissions](../topics/permissions.md)
- [Middleware reference](middleware.md)
- [Settings reference](settings.md)

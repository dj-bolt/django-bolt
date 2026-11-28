---
description: Protect routes with JWT, API keys or Django sessions. Issue, refresh and revoke tokens, and read the user in a handler.
---

# Authentication

Authentication finds out who sent a request.
Django-Bolt checks the credential in Rust before your handler runs.
A guard then decides if the request can continue. See [Permissions](permissions.md).

Bolt has three backends:

| Backend | Credential | Use it for |
| --- | --- | --- |
| `JWTAuthentication` | A signed JSON Web Token in a header or a cookie | Mobile apps, single-page apps, other services |
| `APIKeyAuthentication` | A key from a fixed list, in a header | Scripts and machine clients |
| `SessionAuthentication` | Django's session cookie | Browser apps that log in with Django |

## Protect a route with a JWT

Add `auth` to read the token and `guards` to require it:

```python title="blog/api.py"
import msgspec
from django.contrib.auth import aauthenticate

from django_bolt import BoltAPI, CurrentUser, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user
from django_bolt.exceptions import Unauthorized

api = BoltAPI()


class Credentials(msgspec.Struct):
    username: str
    password: str


@api.post("/auth/token")
async def issue_token(credentials: Credentials) -> dict:
    user = await aauthenticate(username=credentials.username, password=credentials.password)
    if user is None:
        raise Unauthorized(detail="Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    return {"access_token": create_jwt_for_user(user), "token_type": "Bearer"}


@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def me(user: CurrentUser) -> dict:
    return {"id": user.id, "username": user.username}
```

`CurrentUser` loads the Django user of the token. Get a token:

```bash
curl -X POST localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "ada", "password": "correct-horse"}'
```

```json
{"access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiZXhwIjoxNzk1MzE2MzY4...", "token_type": "Bearer"}
```

A request with no token gets a 401. Bolt sends it from Rust, and your handler does not run:

```bash
curl -i localhost:8000/me
```

```http
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
content-type: application/json

{"detail":"Authentication required"}
```

Send the token in the `Authorization` header:

```bash
curl localhost:8000/me -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..."
```

```json
{"id": 1, "username": "ada"}
```

`JWTAuthentication()` with no arguments does these checks:

- It reads the `Authorization` header and removes the `Bearer` scheme and the spaces after it. The scheme name has no case.
- It verifies an `HS256` signature with Django's `SECRET_KEY`. A token with another `alg` fails.
- It requires an `exp` claim and rejects an expired token. It rejects a token whose `nbf` or `iat` is in the future.
- It allows 60 seconds of clock skew on `exp`, `nbf` and `iat`. The `leeway` argument sets this time.
- It rejects a token whose type claim names a refresh token or an ID token. See [Use access and refresh tokens](#use-access-and-refresh-tokens).
- It reads each claim in one JSON type. See [Give each claim its JSON type](#give-each-claim-its-json-type).

Each failure gets the same 401. The body does not tell the client why the token failed.

!!! warning "`auth` alone does not reject a request"
    `auth=[...]` only tries to read a credential. A request with no token, or with a bad token, still reaches the handler.
    Then `request.context` is `None`, and `request.user` is Django's `AnonymousUser`. Add a guard such as `IsAuthenticated()` to reject such a request.

## Issue a token

`create_jwt_for_user()` signs a token for a Django user. It uses `HS256` and `SECRET_KEY` by default, as `JWTAuthentication()` does:

<!-- fragment -->
```python
token = create_jwt_for_user(user, expires_in=900, extra_claims={"role": "editor"})
```

The token has these claims:

| Claim | Value |
| --- | --- |
| `sub` | The primary key of the user, as a string |
| `iat` | The time of issue, in seconds since the epoch |
| `exp` | `iat` plus `expires_in` (default 3600 seconds) |
| `is_staff`, `is_superuser` | The flags of the user |
| `username` | The username |
| `email`, `first_name`, `last_name` | Only when the user has a value |

The token has no `jti` and no `permissions` claim. Add them with `extra_claims`.
A key of `extra_claims` with the name `sub`, `exp`, `iat`, `is_staff` or `is_superuser` raises `ValueError`.

To guard on Django permissions, put them in the token when you issue it:

<!-- fragment -->
```python
permissions = sorted(await user.aget_all_permissions())
token = create_jwt_for_user(user, extra_claims={"permissions": permissions})
```

Bolt checks a guard against the token, not the database. A change to the permissions of a user applies to the next token.

## Read the user in a handler

Bolt gives the result of authentication in three forms.

`request.context` is a dict. Rust fills it, and reading it costs no query:

```python
@api.get("/whoami", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def whoami(request: Request) -> dict:
    return request.context
```

```json
{
  "user_id": "1",
  "is_staff": false,
  "is_superuser": false,
  "auth_backend": "jwt",
  "auth_backend_index": 0,
  "auth_claims": {
    "sub": "1", "exp": 1795316368, "iat": 1795312768,
    "is_staff": false, "is_superuser": false,
    "username": "ada", "email": "ada@example.com"
  }
}
```

The context has a `permissions` list when the token or the API key has permissions. Bolt moves the `permissions` claim there.
The context has `auth_claims` only for a JWT. In `auth_claims`, a custom claim whose value is a JSON array or object is `None`.
The claims `aud` and `amr` keep their lists. A guard still reads the full value of each claim.

`await request.auser()` loads the Django user from the database. It does not block the event loop.
`request.user` is the same user, loaded on the first attribute read. Use it in a sync handler.
A request loads the user one time, also when it reads the user in both ways.

```python
from django_bolt import OptionalCurrentUser


@api.get("/drafts/count", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def count_drafts(request: Request) -> dict:
    user = await request.auser()
    if user is None:  # The user is inactive or deleted.
        raise Unauthorized(detail="Authentication required")
    return {"username": user.username, "drafts": 0}


@api.get("/drafts/owner", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
def drafts_owner(request: Request) -> dict:
    return {"username": request.user.username}


@api.get("/home", auth=[JWTAuthentication()])
async def home(user: OptionalCurrentUser) -> dict:
    return {"signed_in": user is not None}
```

`CurrentUser` gives the user as a parameter and answers 401 when there is no user. `OptionalCurrentUser` gives `None` then.
Both work in sync and async handlers.

Read `request.context` when you need only the user ID.
In the Bolt benchmark, a route that read the JWT context served 155,104 requests per second.
A route that loaded the user served 38,908 (8 processes, 100 connections, September 2026). See [Benchmarks](../benchmarks.md).

A token can be valid for a user that cannot log in. Bolt treats these cases as "no user":

- The user has `is_active = False`.
- The user row does not exist, for example after you deleted the user.

Then `CurrentUser` answers 401, a read such as `request.user.id` answers 401, and `await request.auser()` gives `None`.
A route that does not load the user still accepts the token. To reject the token on each route, [revoke it](#revoke-tokens).

With no credential, `request.user` and `await request.auser()` give the same Django `AnonymousUser`, as Django's `AuthenticationMiddleware` does.
Read `user.is_authenticated` to find out if a user signed in. `AnonymousUser` is true in a boolean test, so `if request.user:` does not work for this.

## Load the user with your own query

By default, Bolt loads the user by the `sub` claim with a primary-key query.
To change the query, subclass the backend and override `get_user`:

```python
from django.contrib.auth.models import User


class EditorJWT(JWTAuthentication):
    async def get_user(self, user_id: str, auth_context: dict):
        return await User.objects.defer("password").filter(pk=user_id, is_active=True).afirst()


@api.get("/dashboard", auth=[EditorJWT()], guards=[IsAuthenticated()])
async def dashboard(user: CurrentUser) -> dict:
    return {"username": user.username}
```

An async `get_user` serves `await request.auser()` only. A sync read of `request.user` then raises `RuntimeError`.
Define `get_user_sync(self, user_id)` too when a sync handler reads `request.user`.
Your method replaces the `is_active` check of Bolt, so do the check in your query.

## Set default authentication for the whole API

Set the defaults in `settings.py`. Each route that does not set `auth` or `guards` uses them:

```python title="settings.py"
from django_bolt.auth import IsAuthenticated, JWTAuthentication

BOLT_AUTHENTICATION_CLASSES = [JWTAuthentication()]
BOLT_DEFAULT_PERMISSION_CLASSES = [IsAuthenticated()]
```

A route can replace either default:

```python
from django_bolt.auth import AllowAny, APIKeyAuthentication


@api.get("/articles")
async def list_articles(request: Request) -> dict:
    return {"user_id": request.context["user_id"]}


@api.get("/health", guards=[AllowAny()])
async def health() -> dict:
    return {"status": "ok"}


@api.get("/feed", auth=[APIKeyAuthentication(api_keys={"sk-feed-2026"})])
async def feed(request: Request) -> dict:
    return {"via": request.context["auth_backend"]}
```

| Request | Response |
| --- | --- |
| `GET /articles` with no token | 401 `{"detail": "Authentication required"}` |
| `GET /articles` with a valid token | 200 `{"user_id": "1"}` |
| `GET /health` | 200 `{"status": "ok"}` |
| `GET /feed` with `X-API-Key: sk-feed-2026` | 200 `{"via": "api_key"}` |
| `GET /feed` with a valid JWT | 401: the route accepts only the API key |

A `Router` takes `auth` and `guards` too. Each route of the router uses them, unless the route sets its own. See [Routing](routing.md).

With `IsAuthenticated()` as the default, give the login route `guards=[AllowAny()]`. Otherwise a client needs a token to get a token.

Without `BOLT_DEFAULT_PERMISSION_CLASSES`, the default guard is `AllowAny()`. Each route is then public until you add a guard.

Without `BOLT_AUTHENTICATION_CLASSES`, a route with no `auth` gets a backend only when it needs one.
A route whose first guard is not `AllowAny()` gets `JWTAuthentication()`.
Other routes get no backend and do no token work. `auth=[]` and `BOLT_AUTHENTICATION_CLASSES = []` mean "no backend".

## Configure JWT verification

`JWTAuthentication` takes keyword arguments. These are the common ones. See the [reference](../ref/auth.md#jwtauthentication) for all of them.

| Argument | Default | Effect |
| --- | --- | --- |
| `secret` | `SECRET_KEY` | The HMAC key that verifies the signature |
| `algorithms` | `["HS256"]` | The `alg` values that Bolt accepts |
| `header` | `"authorization"` | The header that holds the token |
| `cookie` | `None` | Read the token from this cookie, not from the header |
| `audience` | `None` | Require this value in the `aud` claim |
| `issuer` | `None` | Require this value in the `iss` claim |
| `leeway` | `60` | Seconds of clock skew allowed on `exp`, `nbf` and `iat` |

The algorithms are `HS256`, `HS384`, `HS512`, `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384` and `EdDSA`.
All algorithms of one backend must use the same kind of key. An unknown name, or a mix of key kinds, stops startup.

### Give each claim its JSON type

Bolt follows RFC 7515, RFC 7519 and PyJWT. Each claim and header parameter that Bolt reads has one JSON type.
A whole number is an integer, also when the token writes it as `1.0` or `1e0` (RFC 8259, section 6).

| Name | Bolt accepts | Another value, also `null` |
| --- | --- | --- |
| `alg` (header) | A string in `algorithms`, with the same case | The token fails |
| `kid` (header) | A string | The token fails |
| `crit` (header) | No value. Bolt supports no critical extension | The token fails |
| `b64` (header) | Each value but `false`. Bolt reads each payload as base64url (RFC 7797) | `false` fails the token |
| `sub`, `jti` | A string | The token fails |
| `typ`, `token_type`, `token_use` | A string | The token fails |
| `exp`, `nbf`, `iat` | A number from the year 1 to the end of the year 9999 UTC, the range of a Python `datetime`. It can have a fraction | The token fails |
| `iss` | A string | The token fails when the backend has an `issuer`. Else the claim is malformed |
| `aud` | A string, or an array of strings | The token fails when the backend has an `audience`. Else the claim is malformed |
| `is_staff`, `is_superuser`, `is_admin` | `true` or `false` | The claim is malformed. `1` and `"true"` are not `true` |
| `permissions`, `amr` | An array of strings | The claim is malformed |
| `fam` | A string | The claim is malformed |
| `oat`, `ver` | An integer | The claim is malformed |
| Each other claim | Any type | Bolt keeps the value |

A claim has three states: missing, valid and malformed.
A wrong value fails the token when its row in the table says so, also on a route that does not read the claim.
In each other case, the claim is malformed, and Bolt accepts the token, as PyJWT does.
Each part of Bolt that reads a malformed claim fails:

- Each `Requires` guard on the claim refuses the request with its 403. This applies to each form of `Requires`, also `none_of`.
- The claim grants nothing. A malformed `is_staff` or `is_superuser` is `False` in the context. A malformed `permissions` gives no permission.
  A malformed claim does not change the other claims. With `"permissions": "admin"` and `"is_staff": true`, `is_staff` stays `True`.
- `auth_claims` gives the claim as `None`. A missing claim is not in `auth_claims`.
- `rotate_refresh_token()` and the refresh route of `JWTAuthViews` refuse a malformed `fam`, `ver`, `oat` or `amr`. The logout routes refuse a malformed `fam` or `ver`.

A guard with `none_of` also refuses a malformed claim, because the claim can hold a refused value in another form:

<!-- fragment -->
```python
@api.get("/report", auth=[JWTAuthentication()], guards=[Requires("permissions", none_of=["banned"])])
async def report(request):
    # "permissions": ["read"] passes. "permissions": ["banned"] gets 403.
    # "permissions": ["banned", 1] and "permissions": "banned" are malformed. They also get 403.
    # A token with no "permissions" claim passes.
    return {"ok": True}
```

A route that reads no malformed claim accepts the token. A handler that reads `auth_claims` must treat `None` as a failure too.

A claim name that occurs two times fails the token. A header parameter of the table that occurs two times also fails it.
Each of the three parts of the token is base64url with no `=` padding (RFC 7515). A token with padding fails.

For the expiry check, Bolt removes the fraction of `exp`, as PyJWT does. Thus a token with `"exp": 100.5` expires at 100 seconds plus the `leeway`.
With the default `leeway` of 60 seconds, it expires at 160. With `leeway=0`, it expires at 100. `auth_claims` still gives `100.5`.
Bolt reads a decimal as the nearest float, as Python does. Thus the expiry check reads `"exp": 100.99999999` as 100, not 101.
Bolt does not round `nbf` and `iat`. A token is valid from `leeway` seconds before the time that `nbf` or `iat` gives.
In `auth_claims`, a time with a fraction is a `float`. A whole number is an `int`.

### Differences from PyJWT

Bolt refuses each of these tokens, and `jwt.decode` of PyJWT accepts it:

| Token | Reason |
| --- | --- |
| A claim name that occurs two times, or two `alg`, `kid`, `crit` or `b64` header parameters | RFC 7515 and RFC 7519, section 4 allow a parser to refuse it. PyJWT keeps the last value |
| A part with `=` padding | RFC 7515, section 2. PyJWT accepts padding for tokens of AWS ALB |
| `exp`, `nbf` or `iat` as a string or as `true` or `false` | RFC 7519, section 2: a NumericDate is a JSON number. PyJWT reads `"123"` and `true` with `int()` |
| A `typ`, `token_type` or `token_use` that is not a string, or that names `refresh`, `offline` or `id` | Bolt checks the type of each token. PyJWT does not |
| `crit` with `["b64"]` and `"b64": true` | Bolt supports no critical extension. PyJWT supports RFC 7797 |
| `NaN`, `Infinity`, a number beyond the range of a float, a private claim that makes the JSON 128 or more levels deep, or a lone surrogate | RFC 8259 has no `NaN`, and it allows limits on range and depth. A lone surrogate is not a Unicode string. Bolt does not read into a claim that has a type in Bolt, so depth there makes the claim malformed and does not refuse the token |
| A header or claims in UTF-16, or with a UTF-8 byte order mark | RFC 7515, section 5.2 and RFC 7519, section 7.2: each part is UTF-8 JSON. Python's `json` reads the other forms |
| `"jti": ""` on a backend with `require_jti=True` | An empty `jti` names no token. The `require` option of PyJWT checks only that the claim is present |
| `nbf` or `iat` with a fraction, less than one second after now plus `leeway` | PyJWT rounds the claim down, so it accepts the token up to one second early. Bolt does not round |
| `exp`, `nbf` or `iat` before the year 1 or after the year 9999, such as `"exp": 1e300` | A Python `datetime` cannot hold the time, so the revocation stores cannot keep the token. PyJWT reads any number with `int()` |

### Verify tokens from an identity provider

An identity provider such as Auth0, Clerk or Keycloak signs with a private key. Give Bolt the public key:

<!-- fragment -->
```python
JWTAuthentication(public_key=PROVIDER_PUBLIC_KEY_PEM, algorithms=["RS256"], issuer="https://auth.example.com/")
```

Most providers publish their keys as a JSON Web Key Set. Bolt can fetch it:

<!-- fragment -->
```python
JWTAuthentication(
    jwks_url="https://auth.example.com/.well-known/jwks.json",
    algorithms=["RS256"],
    issuer="https://auth.example.com/",
    audience="https://api.example.com",
)
```

`jwks_url` must be an HTTPS URL, and it needs `issuer` and `audience`. Without them, a token for another app of the same provider would pass.
Bolt fetches the key set at startup and selects a key by the `kid` header of the token.
It fetches the set again each `jwks_refresh_interval` seconds (default 300) in the background.
A token with an unknown `kid` starts a fetch at once. After that fetch, an unknown `kid` starts the next fetch after 30 seconds or more.
When a fetch fails, Bolt keeps the last keys.

`oidc_issuer="https://auth.example.com"` reads the issuer, the key set URL and the algorithms from the OpenID Connect discovery document. It needs `audience`.
`jwks={...}` gives the key set directly, for tests or for a server with no network access.

### Read the token from a cookie

A browser app can keep the token in an `HttpOnly` cookie. Pass the cookie name:

```python
browser_jwt = JWTAuthentication(cookie="access_token")


@api.post("/articles", auth=[browser_jwt, JWTAuthentication()], guards=[IsAuthenticated()])
async def create_article(request: Request) -> dict:
    return {"author_id": request.context["user_id"]}
```

With `cookie`, the backend reads only that cookie. The value is the token with no `Bearer ` scheme.
A request that sends the cookie name two times is not authenticated by that backend.

A browser sends cookies with requests from other sites too. Bolt does not run Django's `CsrfViewMiddleware` on its routes.
Thus a cookie backend does its own check, unless you set `csrf=False`.
For a `POST`, `PUT`, `PATCH`, `DELETE` or other unsafe method, the request must come from your own site:

- When the request has `Sec-Fetch-Site`, the value must be `same-origin` or `none`.
- Otherwise the `Origin` header, or else the `Referer` header, must have the scheme and the host of the request.
  A port that is not a number from 0 to 65535 fails, as in a browser.
- A request with none of these headers fails.

A failed check gets 403 `{"detail": "Permission denied"}`. The check applies only when the cookie backend accepted the request.
The route above accepts a script that sends `Authorization: Bearer ...` and no `Origin`.

| Request to `POST /articles` | Status |
| --- | --- |
| Cookie, no `Origin` | 403 |
| Cookie, `Origin: http://testserver` (the same site) | 200 |
| Cookie, `Origin: https://evil.example` | 403 |
| Cookie, `Sec-Fetch-Site: cross-site` | 403 |
| `Authorization: Bearer ...`, no cookie | 200 |

The first backend that accepts a credential wins. In this route, a valid cookie wins over the header, so the CSRF check applies.

## Use access and refresh tokens

A token that lives for days is a risk when someone steals it. The usual answer is two tokens:

- An **access token** lives for minutes. The client sends it with each request.
- A **refresh token** lives for days. The client sends it only to a refresh route, which gives a new pair.

### Add the ready-made routes

`JWTAuthViews` adds four routes that keep both tokens in cookies:

```python
from django_bolt.auth import InMemoryRevocation, JWTAuthViews

JWTAuthViews(store=InMemoryRevocation()).register(api)


@api.get("/drafts", auth=[JWTAuthentication(cookie="access_token")], guards=[IsAuthenticated()])
async def drafts(user: CurrentUser) -> dict:
    return {"author": user.username, "drafts": []}
```

| Route | Effect |
| --- | --- |
| `POST /auth/login` | Checks `{"username", "password"}` with Django's `aauthenticate()`. Sets both cookies. |
| `POST /auth/refresh` | Accepts the refresh cookie. Sets a new pair and marks the old refresh token as used. |
| `POST /auth/logout` | Revokes the refresh token and each token that came from it. Deletes both cookies. |
| `POST /auth/logout-all` | Revokes each refresh token of the user. Deletes both cookies. |

Each route answers `{"ok": true}`. A bad password, an inactive user or a bad refresh token gets 401.
The access token lives 15 minutes and the refresh token 7 days.

The cookies are `HttpOnly`, `Secure` and `SameSite=Lax`. The refresh cookie has the path `/auth`, so the browser sends it only to these routes:

```http
set-cookie: access_token=eyJ...; HttpOnly; SameSite=Lax; Secure; Path=/; Max-Age=900
set-cookie: refresh_token=eyJ...; HttpOnly; SameSite=Lax; Secure; Path=/auth; Max-Age=604800
```

A browser keeps a `Secure` cookie only from an HTTPS site, or from `localhost`. For local HTTP on another host, pass `secure_cookies=False`.
The refresh, logout and logout-all routes do the CSRF check of a cookie backend.

Use `InMemoryRevocation` only in one process. For production, use a store that all processes share. See [Revoke tokens](#revoke-tokens).

!!! warning "An access token stays valid until it expires"
    Logout revokes refresh tokens. Bolt does not look up access tokens in the store, so an access token that the client kept works until its `exp`.
    That is 15 minutes or less with `JWTAuthViews`. Keep access tokens short.

To accept other credentials, pass `credential_validator`, an async callable `(request, credentials) -> user | None`.
To sign with an asymmetric key, pass `algorithm="RS256"`, the private key as `secret` and the public key as `public_key`.

### Build your own refresh route

Use the functions that `JWTAuthViews` uses when you need tokens in the response body, or another login method:

```python
from django_bolt.auth import TokenRotationError, create_token_pair, rotate_refresh_token

store = InMemoryRevocation()


@api.post("/api/login")
async def api_login(credentials: Credentials) -> dict:
    user = await aauthenticate(username=credentials.username, password=credentials.password)
    if user is None:
        raise Unauthorized(detail="Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    version = await store.get_user_version(str(user.pk))
    pair = create_token_pair(user, version=version, method="pwd")
    return {"access_token": pair.access_token, "refresh_token": pair.refresh_token}


@api.post("/api/refresh", auth=[JWTAuthentication(token_type="refresh")], guards=[IsAuthenticated()])
async def api_refresh(request: Request) -> dict:
    try:
        pair = await rotate_refresh_token(request.context["auth_claims"], store=store)
    except TokenRotationError:
        raise Unauthorized(detail="Invalid token") from None
    return {"access_token": pair.access_token, "refresh_token": pair.refresh_token}
```

The access token has the claim `"typ": "access"` and the refresh token `"typ": "refresh"`. Rust uses the claim to keep them apart:

- A route with `token_type="refresh"` accepts only a refresh token.
- A route with no `token_type` rejects a refresh token. It also rejects a token whose `typ`, `token_type` or `token_use` claim is `refresh`, `offline` or `id`, in any case.

`rotate_refresh_token()` marks the old refresh token as used and gives a new pair. Each pair keeps the family of the first login in the `fam` claim.
When a client sends a used refresh token again, Bolt treats it as stolen. It revokes the whole family, and the next refresh of the real client fails too.

| Request | Status |
| --- | --- |
| `POST /api/refresh` with the refresh token | 200, a new pair |
| `POST /api/refresh` with the same refresh token again | 401 `{"detail": "Invalid token"}` |
| `POST /api/refresh` with the new refresh token | 401: the family is revoked |
| `POST /api/refresh` with an access token | 401 `{"detail": "Authentication required"}` |

Catch `TokenRotationError` and answer one uniform 401. Its message tells why the rotation failed, and the client must not learn that.

`rotate_refresh_token()` does not copy custom claims, because they can be out of date. Pass them again with `claims=`.
To end a session some time after the first login, pass `max_session_lifetime` in seconds. Each pair keeps the first login time in the `oat` claim.

### Log a user out everywhere

`await store.bump_user_version(user_id)` adds 1 to the token version of the user.
Each refresh token has the version of its login in the `ver` claim. A refresh token with an old version fails to rotate.
Access tokens still work until they expire.

## Revoke tokens

Give `JWTAuthentication` a revocation store. Bolt then checks the `jti` claim of each token in the store, before the handler runs:

```python
import uuid

from django_bolt.auth import DjangoORMRevocation

revocations = DjangoORMRevocation()
revocable_jwt = JWTAuthentication(revocation_store=revocations)


@api.post("/auth/logout", auth=[revocable_jwt], guards=[IsAuthenticated()])
async def logout(request: Request) -> dict:
    claims = request.context["auth_claims"]
    await revocations.revoke(claims["jti"], exp=claims["exp"])
    return {"logged_out": True}


@api.get("/articles/drafts", auth=[revocable_jwt], guards=[IsAuthenticated()])
async def list_drafts() -> dict:
    return {"drafts": []}


def issue_revocable_token(user) -> str:
    return create_jwt_for_user(user, extra_claims={"jti": uuid.uuid4().hex})
```

After the logout, the same token gets this answer:

```http
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer

{"detail":"Token has been revoked"}
```

A store makes the `jti` claim required. A token with no `jti` gets 401 `{"detail": "Authentication required"}`.
`create_jwt_for_user()` adds no `jti`, so add one with `extra_claims`. `create_token_pair()` adds one to each token.

Pass the `exp` of the token to `revoke()`. The store then keeps the entry until `exp` plus its `leeway` (60 seconds), the last time that the backend accepts the token.
Without `exp`, the entry lives for the `default_ttl` of the store: 7 days.

| Store | Shared by processes | Setup |
| --- | --- | --- |
| `InMemoryRevocation()` | No | None. For tests and one process. |
| `DjangoCacheRevocation(cache_alias="default")` | Yes, with Redis or Memcached | A Django cache |
| `DjangoORMRevocation()` | Yes | Add `"django_bolt.auth.revocation_db"` to `INSTALLED_APPS` and run `migrate` |

Refresh rotation and `bump_user_version()` need an atomic `add` and `incr` in the cache. `DjangoCacheRevocation` refuses the file, database and dummy cache backends for them.
On a database cache, `DjangoCacheRevocation` uses the async cache API, so each check runs its query in a thread.
On each other cache, the store calls the cache on the event loop, with no thread.
`DjangoORMRevocation` does not delete old rows. Delete rows with `expires_at` in the past from a periodic task. Keep the rows whose `jti` starts with `version:`.
A token `jti` cannot start with `family:` or `version:`. The store raises `ReservedJtiError` for it, and the request gets a 401.
A database collation can compare text without case or accents, for example the default collation of MySQL.
Thus the store also refuses a `jti` such as `VERSION:u` or `vérsion:u`. See [RevocationStore](../ref/auth.md#revocationstore).
A `jti` longer than the `max_length` of the `jti` field of the model also gets a 401. For the `RevokedToken` model of Bolt, this limit is 255 characters. On a memcached backend, a `jti` with a space also gets a 401.

When you change the `leeway` of the backend, give the store the same value: `DjangoCacheRevocation(leeway=120)`. A store `leeway` smaller than the backend `leeway` raises `ImproperlyConfigured`.

For your own rule, pass `revoked_token_handler`, an async callable. It gets the `jti`, or the `jti` and the claims when it has two parameters. When it returns `True`, the request gets 401.

## Authenticate with an API key

`APIKeyAuthentication` accepts a key from a fixed set, in the `X-API-Key` header:

```python
import os

from django_bolt.auth import Requires

importer = APIKeyAuthentication(
    api_keys={os.environ["IMPORTER_API_KEY"]},
    key_permissions={os.environ["IMPORTER_API_KEY"]: {"blog.add_article"}},
)


@api.post("/import", auth=[importer], guards=[Requires("permissions", "blog.add_article")])
async def import_articles(request: Request) -> dict:
    return {"imported": 0, "by": request.context["auth_backend"]}
```

The header value is the key, `Bearer <key>` or `ApiKey <key>`. Bolt compares keys in constant time.
The context has `"user_id": "apikey:<key>"`, so do not log the context.
A key has no Django user: `request.user` and `await request.auser()` give `AnonymousUser`.

`api_keys` must be a set, list or tuple of strings. A plain string, an empty key, or a key that starts with a space, `Bearer ` or `ApiKey ` raises `ImproperlyConfigured`.
Thus a missing environment variable stops startup.
`key_permissions` gives the permissions that `Requires("permissions", ...)` checks. Each key in it must be in `api_keys`.
Use `header="authorization"` to read the key from another header.

## Authenticate with a Django session

`SessionAuthentication` uses Django's login and Django's session cookie:

```python
from django.contrib.auth import alogin, alogout

from django_bolt.auth import SessionAuthentication

session = SessionAuthentication()


@api.post("/session/login", auth=[session], guards=[AllowAny()])
async def session_login(request: Request, credentials: Credentials) -> dict:
    user = await aauthenticate(request, username=credentials.username, password=credentials.password)
    if user is None:
        raise Unauthorized(detail="Invalid credentials")
    await alogin(request, user)
    return {"username": user.username}


@api.post("/session/logout", auth=[session], guards=[AllowAny()])
async def session_logout(request: Request) -> dict:
    await alogout(request)
    return {"logged_out": True}


@api.get("/session/me", auth=[session], guards=[IsAuthenticated()])
async def session_me(user: CurrentUser) -> dict:
    return {"username": user.username}
```

Add `"django.contrib.sessions"` to `INSTALLED_APPS`. Bolt adds Django's `SessionMiddleware` and `AuthenticationMiddleware` to each route with this backend.
When the middleware of the API or the router has both, Bolt adds nothing. When it has only one of them, startup fails.

- A request with no session cookie gets 401 from Rust, and no Python runs.
- For a request with the cookie, Django loads the session and the user. Bolt then checks the guards against `request.user`.
- A guard can read `is_staff`, `is_superuser` and `permissions`. `Requires("permissions", ...)` uses the permissions of the user and of its groups.
- `Requires` on another claim rejects a session request with 403.

The CSRF check of a cookie backend applies to each unsafe request to these routes, also with no cookie.
The login request above needs an `Origin` header of your site. A request with no `Origin` gets 403.

Pass `login_url="/accounts/login/"` to redirect an anonymous request with a 302, as Django's `login_required` does.
Each session request runs two queries: one for the session and one for the user. `SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"` reads the session from the cache.

## Accept more than one credential

List more than one backend. Bolt tries them in order and uses the first that accepts the credential:

```python
@api.get(
    "/stats",
    auth=[JWTAuthentication(), APIKeyAuthentication(api_keys={"sk-stats-2026"})],
    guards=[IsAuthenticated()],
)
async def stats(request: Request) -> dict:
    return {"via": request.context["auth_backend"]}
```

`request.context["auth_backend"]` is `"jwt"`, `"api_key"` or `"session"`. A session backend is always tried last.

The 401 of a route names the scheme of each backend in the `WWW-Authenticate` header. The route above sends `www-authenticate: Bearer, ApiKey`.

| Backend | Scheme |
| --- | --- |
| `JWTAuthentication()` with the `Authorization` header | `Bearer` |
| `JWTAuthentication` with another `header` or with `cookie` | `ApiKey` |
| `APIKeyAuthentication` | `ApiKey` |
| `SessionAuthentication` | `ApiKey` |

A 401 that a handler or a dependency raises on the route gets the same header. A route with no backend has no challenge, so set the header in your exception.

## See also

- [Permissions](permissions.md): guards and their 401 and 403 answers.
- [Authentication reference](../ref/auth.md): every class, argument and default.
- [CORS](cors.md): send cookies from a front end on another origin.
- [Rate limiting](rate-limiting.md): limit each user with `key="user"`.
- [Testing](testing.md): send a token with `TestClient`.

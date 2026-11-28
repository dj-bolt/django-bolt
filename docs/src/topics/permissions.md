---
description: Allow or reject a request with guards. Require a login, check roles and permissions in the token, and answer 401 or 403.
---

# Permissions

A guard decides if an authenticated request can reach the handler.
Django-Bolt runs guards in Rust, after [authentication](authentication.md) and before any Python code. The guards of a session request run in Python, after Django loads the user.
A request that fails a guard gets a 401 or a 403, and your handler does not run.

Bolt has three guards:

| Guard | Passes when |
| --- | --- |
| `IsAuthenticated()` | A backend accepted a credential |
| `Requires(claim, ...)` | The credential has the claim, with a value that you expect |
| `AllowAny()` | Always. It also stops the guards after it. |

## Require a logged-in user

Put the guards of a route in `guards`:

```python title="blog/api.py"
from django_bolt import BoltAPI, CurrentUser
from django_bolt.auth import AllowAny, IsAuthenticated, JWTAuthentication, Requires

api = BoltAPI()
jwt = JWTAuthentication()


@api.get("/articles", auth=[jwt], guards=[AllowAny()])
async def list_articles() -> list:
    return [{"id": 1, "title": "Hello"}]


@api.get("/drafts", auth=[jwt], guards=[IsAuthenticated()])
async def list_drafts(user: CurrentUser) -> dict:
    return {"author": user.username, "drafts": []}


@api.post("/articles", auth=[jwt], guards=[Requires("permissions", "blog.add_article")])
async def create_article() -> dict:
    return {"created": True}


@api.get("/admin/stats", auth=[jwt], guards=[Requires("is_staff", True, message="Staff only")])
async def admin_stats() -> dict:
    return {"articles": 12}
```

The token of `ada` has no permissions. The token of `grace` has `"is_staff": true`:

| Request | Status | Body |
| --- | --- | --- |
| `GET /drafts`, no token | 401 | `{"detail": "Authentication required"}` |
| `GET /drafts`, `ada` | 200 | `{"author": "ada", "drafts": []}` |
| `POST /articles`, no token | 401 | `{"detail": "Authentication required"}` |
| `POST /articles`, `ada` | 403 | `{"detail": "Permission denied"}` |
| `GET /admin/stats`, `ada` | 403 | `{"detail": "Staff only"}` |
| `GET /admin/stats`, `grace` | 200 | `{"articles": 12}` |

## Know when a guard answers 401 or 403

- **401 Unauthorized**: the request has no credential, or the credential is not valid. A client can fix it when it logs in.
  The response has a `WWW-Authenticate` header, for example `www-authenticate: Bearer`.
- **403 Forbidden**: the credential is valid, but a `Requires` guard does not match. Logging in again does not help.

`Requires` answers 401, not 403, to a request with no credential. A 401 never tells the client which guard failed.
The `message` of `Requires` sets the `detail` of its 403 only.

A cookie backend also answers 403 to a cross-site request. See [Read the token from a cookie](authentication.md#read-the-token-from-a-cookie).

## Check a claim with `Requires`

`Requires` compares one claim of the credential with the values that you give.
Bolt reads the claim from the verified token, so it does not query the database.

<!-- fragment -->
```python
Requires("tenant_id")                         # The claim is present and not null.
Requires("role", "editor")                    # The claim equals "editor", or a list claim contains it.
Requires("role", "editor", "admin")           # Any of these values.
Requires("is_staff", True)                    # A boolean claim.
Requires("permissions", all_of=["blog.add_article", "blog.change_article"])  # Each value.
Requires("role", none_of=["banned", "suspended"])                           # None of these values.
```

The rules:

- Values in the positional arguments match when the claim matches one of them.
- `all_of` matches when the claim, a list, contains each value.
- `none_of` matches when the claim matches no value. An absent claim matches.
- A typed claim in another JSON type is malformed. It matches no form, also `none_of`, and the guard gives 403.
  See [Give each claim its JSON type](authentication.md#give-each-claim-its-json-type).
- A scalar claim matches a value that is equal. A list claim matches a value that it contains.
- With no values, the claim must be present and not `null`. For a boolean claim, present means `true`.
  Thus `Requires("is_admin")` rejects a token with `"is_admin": false`.
  For `permissions`, present means one permission or more. Thus `Requires("permissions")` rejects a token with `"permissions": []`,
  an API key with no permissions and a session user with no permissions. An empty list gives no permission.
- A value is a `str`, an `int`, a `float` or a `bool`.

Give only one of: values, `all_of`, `none_of`. To require one value and exclude another, use two guards:

<!-- fragment -->
```python
guards=[Requires("permissions", "blog.publish"), Requires("role", none_of=["suspended"])]
```

### Check Django permissions

The `permissions` claim is a list of permission names, as Django's `user.get_all_permissions()` gives them.
Put the list in the token when you issue it:

<!-- fragment -->
```python
token = create_jwt_for_user(user, extra_claims={"permissions": sorted(await user.aget_all_permissions())})
```

Bolt checks the list from the token. A permission that you give the user later applies to the next token.
For an API key, `permissions` reads the `key_permissions` of the key. For a session, it reads the permissions of the Django user.

### Check staff and superuser flags

There is no `IsAdminUser` guard. Check the claims that `create_jwt_for_user()` writes:

<!-- fragment -->
```python
guards=[Requires("is_staff", True)]
guards=[Requires("is_superuser", True)]
```

### Name a guard that you use often

A guard is a value. Assign it to a name:

```python
IsEditor = Requires("role", "editor", "admin", message="Editors only")


@api.post("/articles/{article_id}/publish", auth=[jwt], guards=[IsEditor])
async def publish_article(article_id: int) -> dict:
    return {"id": article_id, "published": True}
```

You cannot write a guard class of your own. Guards run in Rust, so Bolt accepts only `AllowAny`, `IsAuthenticated` and `Requires`.
Another class in `guards` raises `ImproperlyConfigured` at startup.

## Combine guards

A request must pass each guard of the list. Bolt checks them in order and stops at the first failure:

```python
@api.delete(
    "/articles/{article_id}",
    auth=[jwt],
    guards=[IsAuthenticated(), Requires("permissions", "blog.delete_article")],
)
async def delete_article(article_id: int) -> dict:
    return {"deleted": article_id}
```

`AllowAny()` passes at once and skips the guards after it. Put it alone in a list.

## Set guards for the whole API

Set the default guards in `settings.py`. Each route with no `guards` uses them:

```python title="settings.py"
from django_bolt.auth import IsAuthenticated, JWTAuthentication

BOLT_AUTHENTICATION_CLASSES = [JWTAuthentication()]
BOLT_DEFAULT_PERMISSION_CLASSES = [IsAuthenticated()]
```

A route that sets `guards` replaces the default list. Use `guards=[AllowAny()]` for a public route, such as a login route or a health check.

Without `BOLT_DEFAULT_PERMISSION_CLASSES`, the default is `[AllowAny()]`. Each route is then public until you add a guard.

A `Router` takes `guards` too. Each route of the router uses them, unless the route sets its own:

```python
from django_bolt import Router

admin = Router(prefix="/admin", auth=[jwt], guards=[Requires("is_staff", True)])


@admin.get("/users")
async def list_users() -> list:
    return []


api.include_router(admin)
```

## Check data from the database

A guard sees only the claims of the credential. For a rule that needs the database, raise `HTTPException(403)` in a [dependency](dependencies.md).
This example lets only the author of an article edit it:

```python
from typing import Annotated

import msgspec

from blog.models import Article
from django_bolt import Depends
from django_bolt.exceptions import HTTPException, NotFound


class ArticleUpdate(msgspec.Struct):
    title: str


async def own_article(article_id: int, user: CurrentUser) -> Article:
    article = await Article.objects.filter(pk=article_id).afirst()
    if article is None:
        raise NotFound(detail="Article not found")
    if article.author_id != user.pk:
        raise HTTPException(status_code=403, detail="You can edit only your own articles")
    return article


@api.patch("/articles/{article_id}", auth=[jwt], guards=[IsAuthenticated()])
async def update_article(
    article_id: int,
    data: ArticleUpdate,
    article: Annotated[Article, Depends(own_article)],
) -> dict:
    article.title = data.title
    await article.asave(update_fields=["title"])
    return {"id": article.pk, "title": article.title}
```

| Request | Status | Body |
| --- | --- | --- |
| `PATCH /articles/1` by another user | 403 | `{"detail": "You can edit only your own articles"}` |
| `PATCH /articles/1` by the author | 200 | `{"id": 1, "title": "Final"}` |
| `PATCH /articles/99` | 404 | `{"detail": "Article not found"}` |

The guard rejects an anonymous request in Rust. The dependency then runs only for a logged-in user.

## Know what each backend gives a guard

| Claim | JWT | API key | Session |
| --- | --- | --- | --- |
| `IsAuthenticated()` | A valid token with a `sub` claim | A valid key | A logged-in Django user |
| `permissions` | The `permissions` claim | The `key_permissions` of the key | The permissions of the user and its groups |
| `is_staff`, `is_superuser` | The claims of the token | Never match | The fields of the user |
| Other claims | The claims of the token | Never match | Never match |

Rust checks the guards of a JWT or an API key. Rust cannot read a Django session, so Python checks the guards of a session request after Django loads the user.

## See also

- [Authentication](authentication.md): backends, tokens and the user.
- [Authentication reference](../ref/auth.md#guards): `AllowAny`, `IsAuthenticated` and `Requires`.
- [Dependencies](dependencies.md): reusable checks that need the database.
- [Error handling](error-handling.md): change the format of the 401 and 403 bodies.

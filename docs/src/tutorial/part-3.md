---
description: Tutorial part 3. Issue JWT tokens, protect the write routes with JWTAuthentication and IsAuthenticated, and let only the author change an article.
---

# Tutorial part 3: authentication and permissions

At the end of [part 2](part-2.md), any client can change any article.
In this part, you add these rules:

- A client gets a token with a username and a password.
- Only a client with a token can create, change or delete an article.
- The author of a new article is the user of the token.
- Only the author of an article can change or delete it.

Bolt uses [JSON Web Tokens](https://datatracker.ietf.org/doc/html/rfc7519) (JWT) here.
It signs and checks them with the `SECRET_KEY` of Django.

## Create a second user

You need two users to test the last rule. Create `bob` in the Django shell:

```bash
python manage.py shell
```

```pycon
>>> from django.contrib.auth.models import User
>>> User.objects.create_user("bob", password="battery-staple")
<User: bob>
>>> exit()
```

`bob` has the ID `2`.

## Issue a token

Replace the contents of `mysite/api.py`:

```python title="mysite/api.py"
import msgspec
from django.contrib.auth import aauthenticate

from django_bolt import BoltAPI
from django_bolt.auth import create_jwt_for_user
from django_bolt.exceptions import Unauthorized

api = BoltAPI()


class Credentials(msgspec.Struct):
    username: str
    password: str


class Token(msgspec.Struct):
    access_token: str
    token_type: str = "bearer"


@api.get("/hello")
async def hello() -> dict:
    return {"message": "Hello from Django-Bolt"}


@api.post("/token")
async def issue_token(credentials: Credentials) -> Token:
    user = await aauthenticate(username=credentials.username, password=credentials.password)
    if user is None:
        raise Unauthorized(detail="Invalid username or password")
    return Token(access_token=create_jwt_for_user(user))
```

The `/token` route reads a username and a password from the JSON body.
`aauthenticate()` is the async form of Django's `authenticate()`. It checks the password with the authentication backends of Django.
`create_jwt_for_user()` makes a token for the user. The token is valid for one hour.
If the password is wrong, the handler raises `Unauthorized`, and Bolt sends a `401` response.

Send a wrong password:

```bash
curl -i -X POST localhost:8000/token \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "wrong"}'
```

```text
HTTP/1.1 401 Unauthorized
content-length: 41
content-type: application/json

{"detail":"Invalid username or password"}
```

Send the correct password:

```bash
curl -X POST localhost:8000/token \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "correct-horse"}'
```

```json
{"access_token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiZXhwIjoxNzk1MzE3MTU5LCJpYXQiOjE3OTUzMTM1NTksImlzX3N0YWZmIjpmYWxzZSwiaXNfc3VwZXJ1c2VyIjpmYWxzZSwidXNlcm5hbWUiOiJhbGljZSJ9.X3ccn4WNn474-KJSoejB4dhxQg-3Ge4TCaEeXXGjybE","token_type":"bearer"}
```

Your token is different. Keep a token for each user in a shell variable:

```bash
ALICE_TOKEN=$(curl -s -X POST localhost:8000/token \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "correct-horse"}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")

BOB_TOKEN=$(curl -s -X POST localhost:8000/token \
  -H "Content-Type: application/json" \
  -d '{"username": "bob", "password": "battery-staple"}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
```

## Protect the write routes

Replace the contents of `articles/api.py`:

```python title="articles/api.py"
from datetime import datetime
from typing import Annotated

import msgspec
from django.contrib.auth.models import User

from django_bolt import BoltAPI, CurrentUser
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.exceptions import Forbidden, NotFound

from .models import Article

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: Annotated[str, msgspec.Meta(min_length=1, max_length=200)]
    body: str
    published: bool = False


class ArticleOut(msgspec.Struct):
    id: int
    title: str
    body: str
    author_id: int
    published: bool
    created_at: datetime


@api.get("/articles")
async def list_articles(published: bool | None = None, search: str = "") -> list[ArticleOut]:
    articles = Article.objects.all()
    if published is not None:
        articles = articles.filter(published=published)
    if search:
        articles = articles.filter(title__icontains=search)
    return articles


@api.post(
    "/articles",
    status_code=201,
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
)
async def create_article(data: ArticleIn, user: CurrentUser) -> ArticleOut:
    return await Article.objects.acreate(
        title=data.title,
        body=data.body,
        author=user,
        published=data.published,
    )


async def get_article(article_id: int) -> Article:
    try:
        return await Article.objects.aget(id=article_id)
    except Article.DoesNotExist:
        raise NotFound(detail=f"Article {article_id} not found") from None


async def get_own_article(article_id: int, user: User) -> Article:
    article = await get_article(article_id)
    if article.author_id != user.id:
        raise Forbidden(detail="You can only change your own articles")
    return article


@api.get("/articles/{article_id}")
async def get_article_detail(article_id: int) -> ArticleOut:
    return await get_article(article_id)


@api.put(
    "/articles/{article_id}",
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
)
async def update_article(article_id: int, data: ArticleIn, user: CurrentUser) -> ArticleOut:
    article = await get_own_article(article_id, user)
    article.title = data.title
    article.body = data.body
    article.published = data.published
    await article.asave()
    return article


@api.delete(
    "/articles/{article_id}",
    status_code=204,
    auth=[JWTAuthentication()],
    guards=[IsAuthenticated()],
)
async def delete_article(article_id: int, user: CurrentUser) -> None:
    article = await get_own_article(article_id, user)
    await article.adelete()
```

These are the changes from part 2:

- `ArticleIn` has no `author_id` field. The client cannot choose the author.
- The `POST`, `PUT` and `DELETE` routes have `auth` and `guards` arguments.
- These handlers have a `user: CurrentUser` parameter.
- `get_own_article` raises `Forbidden` when the user is not the author.

### Require a token

`auth=[JWTAuthentication()]` tells Bolt to read a token from the `Authorization: Bearer <token>` header.
Bolt checks the signature and the expiry time of the token in Rust, before it calls Python.

`guards=[IsAuthenticated()]` rejects a request that has no valid token. Bolt sends `401`, and the handler does not run.
A guard checks the request before the handler. For other guards, see [Permissions](../topics/permissions.md).

The `GET` routes have no `auth` argument. They stay public.

### Set the author

`CurrentUser` is a dependency. It loads the `User` of the token from the database, and gives it to the handler.
`create_article` uses this user as the author.

### Allow only the author to change an article

A guard can check the data in the token, for example a role or a permission.
It cannot check the author of an article, because that data is in the database.
Thus `update_article` and `delete_article` call `get_own_article`. It loads the article, compares the author with the user, and raises `Forbidden` (`403`) for a different user.

## Try the rules

Create an article with no token:

```bash
curl -i -X POST localhost:8000/articles \
  -H "Content-Type: application/json" \
  -d '{"title": "Signed article", "body": "Written by Alice."}'
```

```text
HTTP/1.1 401 Unauthorized
content-length: 36
www-authenticate: Bearer
content-type: application/json

{"detail":"Authentication required"}
```

A token that is not valid gets the same response:

```bash
curl -i -X POST localhost:8000/articles \
  -H "Authorization: Bearer not-a-token" \
  -H "Content-Type: application/json" \
  -d '{"title": "Forged", "body": "Bad token."}'
```

```text
HTTP/1.1 401 Unauthorized
content-length: 36
www-authenticate: Bearer
content-type: application/json

{"detail":"Authentication required"}
```

Create an article as `alice`:

```bash
curl -i -X POST localhost:8000/articles \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Signed article", "body": "Written by Alice.", "published": true}'
```

```text
HTTP/1.1 201 Created
content-length: 134
content-type: application/json

{"id":3,"title":"Signed article","body":"Written by Alice.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:40.524136Z"}
```

The body has no `author_id`, and the response has `"author_id":1`. Bolt took the author from the token.

Change the article as `bob`:

```bash
curl -i -X PUT localhost:8000/articles/3 \
  -H "Authorization: Bearer $BOB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Edited by Bob", "body": "Bob was here."}'
```

```text
HTTP/1.1 403 Forbidden
content-length: 50
content-type: application/json

{"detail":"You can only change your own articles"}
```

Change the article as `alice`:

```bash
curl -X PUT localhost:8000/articles/3 \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Signed article", "body": "Edited by Alice.", "published": true}'
```

```json
{"id":3,"title":"Signed article","body":"Edited by Alice.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:40.524136Z"}
```

Delete the article as `bob`:

```bash
curl -i -X DELETE localhost:8000/articles/3 \
  -H "Authorization: Bearer $BOB_TOKEN"
```

```text
HTTP/1.1 403 Forbidden
content-length: 50
content-type: application/json

{"detail":"You can only change your own articles"}
```

The list is still public:

```bash
curl localhost:8000/articles
```

```json
[{"id":3,"title":"Signed article","body":"Edited by Alice.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:40.524136Z"},{"id":1,"title":"Hello, Bolt","body":"My first article.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.217932Z"}]
```

## Use the token in the API docs

Open `http://127.0.0.1:8000/docs`.
The `POST`, `PUT` and `DELETE` routes now show a lock icon.

1. Click **Authorize**.
2. Paste the value of `$ALICE_TOKEN` (run `echo $ALICE_TOKEN`), and click **Authorize**.
3. Send a request from a protected route with **Try it out**.

Swagger UI adds the `Authorization` header to each request.

## Summary

- `create_jwt_for_user()` makes a token. `JWTAuthentication` checks it.
- A guard such as `IsAuthenticated()` rejects a request before the handler runs.
- `CurrentUser` gives the user of the token to the handler.
- A rule that needs the database, such as "only the author", goes in the handler or in a dependency.

**Next:** [Part 4: tests and running in production](part-4.md)

## See also

- [Authentication](../topics/authentication.md): JWT options, API keys, sessions and token refresh.
- [Permissions](../topics/permissions.md): `IsAuthenticated`, `AllowAny` and `Requires`.
- [Dependencies](../topics/dependencies.md): reuse code such as `get_own_article` with `Depends`.

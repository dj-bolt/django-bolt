---
description: Tutorial part 2. Add an Article model, validate JSON bodies with msgspec, return QuerySets, send 404 errors and filter with query parameters.
---

# Tutorial part 2: models, validation and the ORM

In this part, you add an `articles` app with these routes:

| Route | Action |
| --- | --- |
| `GET /articles` | List the articles. Filter them with query parameters. |
| `POST /articles` | Create an article. |
| `GET /articles/{article_id}` | Read one article. |
| `PUT /articles/{article_id}` | Change an article. |
| `DELETE /articles/{article_id}` | Delete an article. |

This part starts where [part 1](index.md) stops. Keep `runbolt --dev` running in the first terminal.

## Create the articles app

```bash
python manage.py startapp articles
```

Add `"articles"` to `INSTALLED_APPS`:

```python title="mysite/settings.py"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_bolt",
    "articles",
]
```

## Define the Article model

Replace the contents of `articles/models.py`:

```python title="articles/models.py"
from django.conf import settings
from django.db import models


class Article(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="articles",
    )
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
```

This is a usual Django model. Bolt does not need a base class or a mixin.

Create and apply the migration:

```bash
python manage.py makemigrations articles
python manage.py migrate
```

```text
Migrations for 'articles':
  articles/migrations/0001_initial.py
    + Create model Article
Operations to perform:
  Apply all migrations: admin, articles, auth, contenttypes, sessions
Running migrations:
  Applying articles.0001_initial... OK
```

## Create a user

Each article has an author. Create a user with the name `alice` in the Django shell:

```bash
python manage.py shell
```

```pycon
>>> from django.contrib.auth.models import User
>>> User.objects.create_user("alice", password="correct-horse")
<User: alice>
>>> exit()
```

In a new database, `alice` has the ID `1`.

## List the articles

Create the file `articles/api.py`:

```python title="articles/api.py"
from datetime import datetime
from typing import Annotated

import msgspec

from django_bolt import BoltAPI
from django_bolt.exceptions import NotFound

from .models import Article

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: Annotated[str, msgspec.Meta(min_length=1, max_length=200)]
    body: str
    author_id: int
    published: bool = False


class ArticleOut(msgspec.Struct):
    id: int
    title: str
    body: str
    author_id: int
    published: bool
    created_at: datetime


@api.get("/articles")
async def list_articles() -> list[ArticleOut]:
    return Article.objects.all()
```

The file defines two schemas. A schema is a `msgspec.Struct` that sets the shape of the data:

- `ArticleIn` is the data that a client sends. `msgspec.Meta(min_length=1, max_length=200)` adds limits to `title`. `published` has a default value, so the client can leave it out.
- `ArticleOut` is the data that the API sends back.

The return type of `list_articles` is `list[ArticleOut]`.
The handler returns a QuerySet, and Bolt makes the JSON list from it.
Bolt runs the query in a thread pool, so the query does not block the server.
For each row, Bolt reads the fields of `ArticleOut` only.

The file has its own `api = BoltAPI()`.
`runbolt` finds `articles/api.py` and adds its routes to the routes of `mysite/api.py`.
The `NotFound` import is for a later step.

Save the file. The server log shows the reload:

```text
  02:12:32 [bolt] reloading articles/api.py
System check identified no issues (0 silenced).
  ✓ Reloaded in 407 ms  http://127.0.0.1:8000
```

Send a request. The database has no articles yet:

```bash
curl localhost:8000/articles
```

```json
[]
```

## Create an article

Add this route to the end of `articles/api.py`:

```python title="articles/api.py"
@api.post("/articles", status_code=201)
async def create_article(data: ArticleIn) -> ArticleOut:
    return await Article.objects.acreate(
        title=data.title,
        body=data.body,
        author_id=data.author_id,
        published=data.published,
    )
```

The parameter `data: ArticleIn` tells Bolt to read the JSON body of the request.
Bolt validates the body before it calls the handler.
`status_code=201` sets the status of a successful response.
The handler returns an `Article` instance, and Bolt converts it to `ArticleOut`.

Create two articles:

```bash
curl -X POST localhost:8000/articles \
  -H "Content-Type: application/json" \
  -d '{"title": "Hello, Bolt", "body": "My first article.", "author_id": 1, "published": true}'
```

```json
{"id":1,"title":"Hello, Bolt","body":"My first article.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.217932Z"}
```

```bash
curl -X POST localhost:8000/articles \
  -H "Content-Type: application/json" \
  -d '{"title": "Draft notes", "body": "Not ready yet.", "author_id": 1}'
```

```json
{"id":2,"title":"Draft notes","body":"Not ready yet.","author_id":1,"published":false,"created_at":"2026-11-22T02:12:35.226507Z"}
```

Your `created_at` values are different.

Send an empty title. Bolt rejects the body with the status `422 Unprocessable Entity`:

```bash
curl -i -X POST localhost:8000/articles \
  -H "Content-Type: application/json" \
  -d '{"title": "", "body": "An empty title.", "author_id": 1}'
```

```text
HTTP/1.1 422 Unprocessable Entity
content-length: 101
content-type: application/json

{"detail":[{"loc":["body","title"],"msg":"Expected `str` of length >= 1","type":"validation_error"}]}
```

Leave out a required field:

```bash
curl -X POST localhost:8000/articles \
  -H "Content-Type: application/json" \
  -d '{"title": "No body", "author_id": 1}'
```

```json
{"detail":[{"loc":["body","body"],"msg":"Object missing required field `body`","type":"missing_field"}]}
```

`loc` tells the client where the error is. The handler did not run, so the database has no new row.

List the articles again. The model sets `ordering = ["-created_at"]`, so the newest article is first:

```bash
curl localhost:8000/articles
```

```json
[{"id":2,"title":"Draft notes","body":"Not ready yet.","author_id":1,"published":false,"created_at":"2026-11-22T02:12:35.226507Z"},{"id":1,"title":"Hello, Bolt","body":"My first article.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.217932Z"}]
```

## Read one article

Add a helper function and a route to the end of `articles/api.py`:

```python title="articles/api.py"
async def get_article(article_id: int) -> Article:
    try:
        return await Article.objects.aget(id=article_id)
    except Article.DoesNotExist:
        raise NotFound(detail=f"Article {article_id} not found") from None


@api.get("/articles/{article_id}")
async def get_article_detail(article_id: int) -> ArticleOut:
    return await get_article(article_id)
```

`{article_id}` in the path is a path parameter.
The handler has a parameter with the same name, and its type hint `int` tells Bolt to convert the value.

`get_article` is not a route. It is a function that the handlers call.
When no row matches, it raises `NotFound`. Bolt sends this exception as a `404` response.
You use `get_article` again in the update and delete routes.

```bash
curl localhost:8000/articles/1
```

```json
{"id":1,"title":"Hello, Bolt","body":"My first article.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.217932Z"}
```

Ask for an article that does not exist:

```bash
curl -i localhost:8000/articles/99
```

```text
HTTP/1.1 404 Not Found
content-length: 33
content-type: application/json

{"detail":"Article 99 not found"}
```

Send a value that is not an integer:

```bash
curl -i localhost:8000/articles/abc
```

```text
HTTP/1.1 422 Unprocessable Entity
content-length: 94
content-type: application/json

{"detail":"Path parameter 'article_id': Invalid integer 'abc': invalid digit found in string"}
```

For the other exceptions and the format of error responses, see [Error handling](../topics/error-handling.md).

## Filter the list with query parameters

Replace the `list_articles` handler with this version:

```python title="articles/api.py"
@api.get("/articles")
async def list_articles(published: bool | None = None, search: str = "") -> list[ArticleOut]:
    articles = Article.objects.all()
    if published is not None:
        articles = articles.filter(published=published)
    if search:
        articles = articles.filter(title__icontains=search)
    return articles
```

A handler parameter that is not in the path is a query parameter.
Each parameter has a default value, so the client can leave it out.
The handler adds a filter to the QuerySet only for a parameter that the client sent.

Show the published articles only:

```bash
curl "localhost:8000/articles?published=true"
```

```json
[{"id":1,"title":"Hello, Bolt","body":"My first article.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.217932Z"}]
```

Search in the titles:

```bash
curl "localhost:8000/articles?search=draft"
```

```json
[{"id":2,"title":"Draft notes","body":"Not ready yet.","author_id":1,"published":false,"created_at":"2026-11-22T02:12:35.226507Z"}]
```

Bolt converts `published` to a `bool`, and rejects a value that is not a boolean:

```bash
curl "localhost:8000/articles?published=maybe"
```

```json
{"detail":"Query parameter 'published': Invalid boolean 'maybe': expected true/false/1/0/yes/no/on/off"}
```

A long list needs pages. See [Pagination](../topics/pagination.md).

## Change and delete an article

Add the last two routes to the end of `articles/api.py`:

```python title="articles/api.py"
@api.put("/articles/{article_id}")
async def update_article(article_id: int, data: ArticleIn) -> ArticleOut:
    article = await get_article(article_id)
    article.title = data.title
    article.body = data.body
    article.author_id = data.author_id
    article.published = data.published
    await article.asave()
    return article


@api.delete("/articles/{article_id}", status_code=204)
async def delete_article(article_id: int) -> None:
    article = await get_article(article_id)
    await article.adelete()
```

`PUT` replaces the fields of the article with the body.
`DELETE` returns `None` and has `status_code=204`, so the response has no body.

Publish the draft:

```bash
curl -X PUT localhost:8000/articles/2 \
  -H "Content-Type: application/json" \
  -d '{"title": "Draft notes", "body": "Ready now.", "author_id": 1, "published": true}'
```

```json
{"id":2,"title":"Draft notes","body":"Ready now.","author_id":1,"published":true,"created_at":"2026-11-22T02:12:35.226507Z"}
```

Delete it:

```bash
curl -i -X DELETE localhost:8000/articles/2
```

```text
HTTP/1.1 204 No Content
```

```bash
curl localhost:8000/articles/2
```

```json
{"detail":"Article 2 not found"}
```

Open `http://127.0.0.1:8000/docs`. It shows the five new routes, with the schemas of `ArticleIn` and `ArticleOut`.

??? example "The complete `articles/api.py`"

    ```python title="articles/api.py"
    from datetime import datetime
    from typing import Annotated

    import msgspec

    from django_bolt import BoltAPI
    from django_bolt.exceptions import NotFound

    from .models import Article

    api = BoltAPI()


    class ArticleIn(msgspec.Struct):
        title: Annotated[str, msgspec.Meta(min_length=1, max_length=200)]
        body: str
        author_id: int
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


    @api.post("/articles", status_code=201)
    async def create_article(data: ArticleIn) -> ArticleOut:
        return await Article.objects.acreate(
            title=data.title,
            body=data.body,
            author_id=data.author_id,
            published=data.published,
        )


    async def get_article(article_id: int) -> Article:
        try:
            return await Article.objects.aget(id=article_id)
        except Article.DoesNotExist:
            raise NotFound(detail=f"Article {article_id} not found") from None


    @api.get("/articles/{article_id}")
    async def get_article_detail(article_id: int) -> ArticleOut:
        return await get_article(article_id)


    @api.put("/articles/{article_id}")
    async def update_article(article_id: int, data: ArticleIn) -> ArticleOut:
        article = await get_article(article_id)
        article.title = data.title
        article.body = data.body
        article.author_id = data.author_id
        article.published = data.published
        await article.asave()
        return article


    @api.delete("/articles/{article_id}", status_code=204)
    async def delete_article(article_id: int) -> None:
        article = await get_article(article_id)
        await article.adelete()
    ```

## Summary

- A `msgspec.Struct` parameter reads and validates the JSON body.
- A return type converts a model instance or a QuerySet to JSON.
- `NotFound` sends a `404` response.
- Parameters with default values are optional query parameters.

The API has a problem: any client can create, change and delete any article, and can choose the author.

**Next:** [Part 3: authentication and permissions](part-3.md)

## See also

- [Requests](../topics/requests.md): path, query, header, cookie and body parameters.
- [Responses](../topics/responses.md): status codes, headers and other response types.
- [Async ORM](../topics/async-orm.md): QuerySets in async handlers.

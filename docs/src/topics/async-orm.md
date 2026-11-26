---
description: Query the Django ORM from async and sync handlers, return QuerySets, avoid N+1 queries, use transactions and tune the ORM thread pools.
---

# Async ORM

Use the Django ORM in Bolt handlers as in Django views. An `async def` handler uses the async ORM API (`aget()`, `acreate()`, `async for`) or returns a lazy QuerySet. A `def` handler uses the sync ORM API, and Bolt runs it on a thread pool.

## Return a QuerySet

The most common read handler builds a QuerySet and returns it. Bolt evaluates the QuerySet on a bounded ORM thread pool, so the query does not block the event loop.

The examples on this page use these models:

```python title="blog/models.py"
from django.conf import settings
from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)


class Article(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="articles")
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="articles")


class Comment(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="comments")
    author_name = models.CharField(max_length=100)
    text = models.TextField()
```

```python title="blog/api.py"
import msgspec

from django_bolt import BoltAPI

from blog.models import Article

api = BoltAPI()


class ArticleItem(msgspec.Struct):
    id: int
    title: str
    published: bool


@api.get("/articles")
async def list_articles() -> list[ArticleItem]:
    return Article.objects.filter(published=True).order_by("id")
```

```bash
curl localhost:8000/articles
```

```json
[{"id": 1, "title": "Hello Bolt", "published": true}, {"id": 2, "title": "Async ORM", "published": true}]
```

The return type tells Bolt how to read the rows:

| Return type | How Bolt reads the QuerySet |
| --- | --- |
| `list[SomeStruct]` (a `msgspec.Struct`) | `.values()` of the Struct fields. A nested Struct of a foreign key or a one-to-one relation reads the related row in the same query. |
| `list[SomeSerializer]` (a Bolt `Serializer`) | Model instances, with `select_related()`, `prefetch_related()` and `annotate()` from the fields. See [Serializers](serializers.md#return-a-queryset). |
| no return type | Return a `.values()` QuerySet. A QuerySet of model instances gives a `500` error, because JSON has no encoding for a model. |

A nested Struct reads only a foreign key or a one-to-one relation. A `None` relation gives `null`, so type the field `AuthorOut | None` when the key can be empty.
For a list of related rows, such as the tags of an article, use a Bolt `Serializer` as the item type. A Struct field for such a relation gives a `TypeError` that names the field.

Bolt also loads the relations of a nested Struct for model instances. This applies to a page of `@paginate`, to a model instance that a handler returns, and to a list of model instances. Each relation costs one query for all the instances. A relation that the QuerySet loads with `select_related()` costs no query.

## Read and write single rows

In an `async def` handler, call the async form of each QuerySet method. It has the prefix `a`:

```python
import msgspec

from django_bolt import BoltAPI
from django_bolt.exceptions import NotFound

from blog.models import Article

api = BoltAPI()


class ArticleIn(msgspec.Struct):
    title: str
    body: str


@api.post("/authors/{author_id}/articles", status_code=201)
async def create_article(author_id: int, data: ArticleIn) -> dict:
    article = await Article.objects.acreate(author_id=author_id, title=data.title, body=data.body)
    return {"id": article.id}


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> dict:
    try:
        article = await Article.objects.aget(id=article_id)
    except Article.DoesNotExist:
        raise NotFound(detail="Article not found")
    return {"id": article.id, "title": article.title}


@api.delete("/articles/{article_id}")
async def delete_article(article_id: int) -> dict:
    deleted, _ = await Article.objects.filter(id=article_id).adelete()
    return {"deleted": deleted}
```

```bash
curl localhost:8000/articles/99
```

```json
{"detail": "Article not found"}
```

| Sync | Async |
| --- | --- |
| `get()`, `first()`, `last()` | `aget()`, `afirst()`, `alast()` |
| `create()`, `get_or_create()`, `update_or_create()` | `acreate()`, `aget_or_create()`, `aupdate_or_create()` |
| `update()`, `delete()` | `aupdate()`, `adelete()` |
| `count()`, `exists()`, `aggregate()` | `acount()`, `aexists()`, `aaggregate()` |
| `bulk_create()`, `bulk_update()` | `abulk_create()`, `abulk_update()` |
| `instance.save()`, `instance.delete()` | `await instance.asave()`, `await instance.adelete()` |
| `for row in qs` | `async for row in qs` |

A sync ORM call in an `async def` handler raises Django's `SynchronousOnlyOperation`, and the client gets a `500` response. This also applies to a relation that is not loaded, for example `article.author` after `aget()`.

Bolt sends each async ORM call to the ORM thread pool. Thus `DJANGO_BOLT_ORM_THREADS` limits the queries in flight. See [Tune the thread pools](#tune-the-thread-pools).
On PostgreSQL, `aget()` and `async for` can run with no thread. See [Run async reads on the event loop](#run-async-reads-on-the-event-loop).

## Iterate over a QuerySet

Use `async for`, or an async comprehension:

```python
from django_bolt import BoltAPI

from blog.models import Article

api = BoltAPI()


@api.get("/titles")
async def list_titles() -> list[str]:
    return [article.title async for article in Article.objects.filter(published=True).order_by("id")]
```

```json
["Hello Bolt", "Async ORM"]
```

`async for` loads all rows in one call to the ORM thread pool. It then iterates over them in memory.
A returned QuerySet with a `list[Struct]` return type reads only the Struct fields, with `.values()`, and makes no model instances. Return the QuerySet when the rows need no change.

## Run async reads on the event loop

Django's async ORM runs each call on a thread. On PostgreSQL, Bolt can run `async for` and `aget()` on the event loop, with no thread. Add the setting:

```python title="blog/settings.py"
BOLT_ASYNC_ORM_ON_LOOP = True
```

Your handlers do not change. Bolt reads the setting when the server starts. Each `async for` and each `aget()` then has three steps:

1. Django builds the SQL statement.
2. Bolt sends the statement on a connection of the event loop. The event loop serves other requests while the database works.
3. Django builds the rows and the model instances.

The result is the result of the sync ORM. The setting is off by default.
It needs PostgreSQL, psycopg 3 and the `django.db.backends.postgresql` engine. It has no effect on Windows.

### Reads that stay on the ORM thread pool

Some reads need the database connection of Django. For these reads, Bolt uses the ORM thread pool, as with the setting off:

| Read | Cause |
| --- | --- |
| A QuerySet with `prefetch_related()` | The prefetch queries are sync code. |
| A read on a route with Django middleware | The thread of the request keeps state, for example the schema of a tenant. |
| A read in a transaction | Only the connection of the transaction has its rows. A `TestClient` in a Django `TestCase` gives such a connection to the handlers. |
| A read with `DEBUG = True` | Django logs each query from its own cursor. |
| A database with another `ENGINE`, for example PostGIS or django-tenants | Another backend can change how a connection opens. |
| The other async calls, for example `afirst()`, `acount()`, `acreate()` and `asave()` | Bolt replaces only `async for` and `aget()`. |

### Connections of the event loop

A loop connection is not a connection of Django. Each worker thread has its own loop connections for each database.

- A loop connection opens with the connection parameters of `DATABASES`.
- A loop connection copies the session settings and the type adapters of a Django connection to the same database. The changes of a `connection_created` receiver are thus on it.
- A read takes a free connection. With no free connection, the loop opens one, up to the limit. At the limit, the read waits. The pool of [sqlx](https://github.com/launchbadge/sqlx) has the same rules and the same defaults.

| Variable | Default | Effect |
| --- | --- | --- |
| `DJANGO_BOLT_LOOP_CONNECTIONS` | `10` | The limit: connections of one worker thread to one database. |
| `DJANGO_BOLT_LOOP_ACQUIRE_SECONDS` | `30` | A read that waits this long for a free connection fails with Django's `OperationalError`. |
| `DJANGO_BOLT_LOOP_IDLE_SECONDS` | `600` | A connection that no read used for this time closes. |
| `DJANGO_BOLT_LOOP_LIFETIME_SECONDS` | `1800` | A connection older than this time closes before its next use. |

- `CONN_MAX_AGE` does not apply to a loop connection, and Django's connection pool (`OPTIONS["pool"]`) does not hold it.
- After a database restart, Bolt sends a read that failed on a dead connection one more time, on a new connection.
- A tool that records queries from the cursor of Django does not see these reads.

Add the loop connections to the connection count of your database. See [Tune the thread pools](#tune-the-thread-pools).

### Set the number of loop connections

One connection runs one query at a time. Thus a worker thread needs this number of connections:

```text
connections = reads in one second x seconds that one read holds a connection
```

A read holds its connection for the round trip to the database, and for the time until the event loop reads the answer.
A loop opens connections up to the limit when that number of reads run at the same time. A higher limit thus does not always give more throughput.

Measured with one process, one route and a database 2 ms away:

| `DJANGO_BOLT_LOOP_CONNECTIONS` | Requests in one second |
| --- | --- |
| `4` | 1,075 |
| `16` | 2,156 |
| `64` | 2,250 |

With `64`, the loop kept 64 connections open for 4% more throughput than with `16`.
With `4`, the ORM thread pool with 4 threads was faster in this test (1,323). With `16`, it was slower (1,377).

```bash
DJANGO_BOLT_LOOP_CONNECTIONS=16 python manage.py runbolt --processes 4
```

## Avoid N+1 queries

A loop that reads a relation of each row sends one query for each row. Load the relations with the first query:

- `select_related("author")` for a `ForeignKey` or a `OneToOneField`. Django adds a SQL join.
- `prefetch_related("tags")` for a `ManyToManyField` or a reverse relation. Django sends one more query for all rows.

```python
from django_bolt import BoltAPI

from blog.models import Article

api = BoltAPI()


@api.get("/articles/{article_id}/card")
async def article_card(article_id: int) -> dict:
    article = await Article.objects.select_related("author").prefetch_related("tags").aget(id=article_id)
    return {
        "title": article.title,
        "author": article.author.username,
        "tags": [tag.name for tag in article.tags.all()],
    }
```

```json
{"title": "Hello Bolt", "author": "alice", "tags": ["django", "rust"]}
```

Without `select_related("author")`, the read of `article.author` raises `SynchronousOnlyOperation`. An async handler cannot load a relation lazily. Thus a missing load gives an error, not a slow page.

A Bolt `Serializer` as the item type of a `list[...]` return type adds these loads for you. See [Serializers](serializers.md#return-a-queryset).

## Write a sync handler

A `def` handler can use the sync ORM API. Bolt reads the source of the handler at startup. A handler that calls the ORM or a blocking function runs on the default thread pool, not on the event loop:

```python
import threading

from django_bolt import BoltAPI

from blog.models import Article

api = BoltAPI()


@api.get("/articles/{article_id}/sync")
def get_article_sync(article_id: int) -> dict:
    article = Article.objects.select_related("author").get(id=article_id)
    return {"title": article.title, "author": article.author.username, "thread": threading.current_thread().name}
```

```json
{"title": "Hello Bolt", "author": "alice", "thread": "bolt_default_0"}
```

A sync handler that only returns a lazy QuerySet does not run a query itself. Bolt evaluates the QuerySet on the ORM thread pool, as for an async handler.

The detection reads only the source of the handler. It does not see a query in a helper function. Such a handler runs on the worker thread that accepted the request. The query works, but the worker serves no other request until the handler returns. Call the ORM in the handler itself, or use an `async def` handler.

A sync handler on a route with Django middleware always runs off the event loop, on the thread of its request. See [Middleware](middleware.md).

## Use a transaction

Django has no async `atomic()`. Put the transaction in sync code.

In a sync handler, use `transaction.atomic()` as in a Django view:

```python
import msgspec
from django.db import transaction

from django_bolt import BoltAPI

from blog.models import Article, Comment

api = BoltAPI()


class CommentIn(msgspec.Struct):
    author_name: str
    text: str


@api.post("/articles/{article_id}/comments", status_code=201)
def add_comment(article_id: int, data: CommentIn) -> dict:
    with transaction.atomic():
        article = Article.objects.select_for_update().get(id=article_id)
        Comment.objects.create(article=article, author_name=data.author_name, text=data.text)
        return {"comments": article.comments.count()}
```

```json
{"comments": 3}
```

In an async handler, put the transaction in a sync function and call it with `sync_to_async()` from asgiref:

```python
import msgspec
from asgiref.sync import sync_to_async
from django.db import transaction

from django_bolt import BoltAPI

from blog.models import Article, Comment

api = BoltAPI()


class CommentIn(msgspec.Struct):
    author_name: str
    text: str


def add_comment_and_count(article_id: int, data: CommentIn) -> int:
    with transaction.atomic():
        article = Article.objects.select_for_update().get(id=article_id)
        Comment.objects.create(article=article, author_name=data.author_name, text=data.text)
        return article.comments.count()


@api.post("/articles/{article_id}/comments", status_code=201)
async def add_comment(article_id: int, data: CommentIn) -> dict:
    count = await sync_to_async(add_comment_and_count)(article_id, data)
    return {"comments": count}
```

All queries of the transaction run in the function, on one thread and one connection.

Bolt does not wrap a handler in a transaction. The `ATOMIC_REQUESTS` database setting has no effect on Bolt routes.

## Read the current user

`request.user` loads the user with a sync query on the first read. In an `async def` handler, this read blocks the event loop for the time of the query. Use one of the non-blocking forms:

```python
from django_bolt import BoltAPI, CurrentUser, Request
from django_bolt.auth import IsAuthenticated, JWTAuthentication

api = BoltAPI()


@api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def me(request: Request) -> dict:
    user = await request.auser()
    return {"username": user.username}


@api.get("/me/short", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def me_short(user: CurrentUser) -> dict:
    return {"username": user.username}
```

```json
{"username": "alice"}
```

- `await request.auser()` loads the user on the ORM thread pool. A later read of `request.user` gets the same object with no query.
- `CurrentUser` is a dependency that gives the user, or a `401` response when there is none. `OptionalCurrentUser` gives `None` in that case.
- In a `def` handler, read `request.user`. The handler already runs off the event loop.

Bolt does not load the user before the handler. A handler that does not read the user sends no user query. With `DEBUG = True` or `runbolt --dev`, Bolt logs each line where a sync read of `request.user` blocks the event loop for 50 ms or more.

See [Authentication](authentication.md) for the auth backends.

## Tune the thread pools

Bolt has two bounded thread pools in each process. Set their sizes with environment variables:

| Variable | Default | Runs |
| --- | --- | --- |
| `DJANGO_BOLT_ORM_THREADS` | `1` when each database is SQLite, else `4` | Returned QuerySets, pagination queries, Django's async ORM calls, `await request.auser()` |
| `DJANGO_BOLT_EXECUTOR_THREADS` | `min(32, CPU count + 4)` | Sync handlers that block, and `django_bolt.concurrency.sync_to_thread()` |

Each value must be an integer of 1 or more. `runbolt` checks both variables at startup and stops with `ImproperlyConfigured` for a bad value.

```bash
DJANGO_BOLT_ORM_THREADS=8 python manage.py runbolt --processes 4
```

On a route with Django middleware, the ORM calls of a request run on the thread of that request, not on the ORM pool. See [Middleware](middleware.md).

Each thread that runs a query keeps its own database connection. These are the threads of the two pools, the worker threads, and one thread for each running request with Django middleware. Add up the threads of all processes. Keep the total below the connection limit of your database.
With `BOLT_ASYNC_ORM_ON_LOOP`, each worker thread also has up to `DJANGO_BOLT_LOOP_CONNECTIONS` connections (10 by default) to each PostgreSQL database.

SQLite gets one ORM thread by default, because SQLite has one writer and a file lock. PostgreSQL and MySQL can serve parallel queries, so they get four.

To run your own blocking call from an async handler, use `sync_to_thread()`:

```python
from django_bolt import BoltAPI
from django_bolt.concurrency import sync_to_thread

api = BoltAPI()


def render_report(year: int) -> dict:
    return {"year": year, "rows": 0}


@api.get("/reports/{year}")
async def report(year: int) -> dict:
    return await sync_to_thread(render_report, year)
```

```json
{"year": 2026, "rows": 0}
```

`sync_to_thread()` uses the default pool. Do not make another thread pool for ORM work.

## Manage database connections

Bolt does not close database connections at the end of a request. Each thread keeps its connection for the next request.

- With `CONN_MAX_AGE` or `CONN_HEALTH_CHECKS` set, Bolt runs Django's age and health checks before each call on a pool thread.
- After an error, Bolt closes each connection of that thread that Django reports as unusable. A dead connection thus fails one request, not each later request.
- With Django's connection pool (`OPTIONS["pool"]` on PostgreSQL), a thread gives its connection back to the pool when it has no more work. Install `django-bolt[pool]` for `psycopg_pool` 3.3 or later. Bolt then drains the pool when it finds a dead connection.

## See also

- [Serializers](serializers.md)
- [Pagination](pagination.md)
- [Authentication](authentication.md)
- [Middleware](middleware.md)
- [Settings reference](../ref/settings.md)
- [Django: Asynchronous support](https://docs.djangoproject.com/en/stable/topics/async/)

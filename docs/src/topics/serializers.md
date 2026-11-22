---
description: Validate input and shape output with the Bolt Serializer, with validators, computed fields, field sets, nesting and Django model integration.
---

# Serializers

A Bolt `Serializer` is a `msgspec.Struct` with more features.
It adds field and model validators, computed fields, input-only and output-only fields, named field sets, and conversion from and to Django models.

## Choose a Serializer or a msgspec.Struct

Bolt accepts both types as a request body and as a response type.
A plain `msgspec.Struct` runs no Python code during validation. A `Serializer` runs Python code for its validators and for `from_model()`.
Use a `Serializer` when you need one of these features:

| You need | Use |
| --- | --- |
| Typed fields and constraints only | `msgspec.Struct` |
| A validation rule in Python code | `Serializer` with `@field_validator` or `@model_validator` |
| Output from a model instance, also with relations | `Serializer` with `from_model()` |
| A field that only input has, or only output has | `Serializer` with `field(write_only=True)` or `field(read_only=True)` |
| A value that a method calculates | `Serializer` with `@computed_field` |
| Several output shapes from one class | `Serializer` with `Config.field_sets` |
| A list response from a QuerySet with nested relations | `Serializer` |

Import `Serializer` and its helpers from `django_bolt.serializers`.

## Define a serializer

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

Declare each field with a type hint. Put constraints in `Annotated[..., Meta(...)]`, as for a `msgspec.Struct`:

```python title="blog/api.py"
from datetime import datetime
from typing import Annotated

from msgspec import Meta

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class ArticleIn(Serializer):
    title: Annotated[str, Meta(min_length=3, max_length=200)]
    body: str
    author: int
    published: bool = False


class ArticleOut(Serializer):
    id: int
    title: str
    author: int
    published: bool
    created_at: datetime


@api.post("/articles", status_code=201)
async def create_article(data: ArticleIn) -> ArticleOut:
    article = data.to_model(Article)
    await article.asave()
    return ArticleOut.from_model(article)


@api.get("/articles/{article_id}")
async def get_article(article_id: int) -> ArticleOut:
    return await Article.objects.aget(id=article_id)
```

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"title": "Bolt serializers", "body": "Text", "author": 1}'
```

```json
{"id": 4, "title": "Bolt serializers", "author": 1, "published": false, "created_at": "2026-11-22T02:06:08.807076Z"}
```

The field `author: int` holds the primary key of the related user. `to_model()` writes it to `author_id`, and `from_model()` reads it from `author_id` with no query.

The second handler returns a model instance. The return type is a `Serializer`, so Bolt calls `from_model()` and `dump()` for you.

A `Serializer` is keyword-only. Create an instance with keyword arguments: `ArticleIn(title="Hello", body="Text", author=1)`. Thus a required field can come after a field with a default.

A body that does not match the types gives a `422` response. msgspec stops at the first type or constraint error:

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"title": "Hi", "body": "Text", "author": 1}'
```

```json
{"detail": [{"loc": ["body", "title"], "msg": "Expected `str` of length >= 3", "type": "validation_error"}]}
```

## Validate one field

Add a method with `@field_validator("<field name>")`. The method gets the class and the value. Return the value to keep. Raise `ValueError` or `TypeError` to reject the value:

```python
from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, field_validator

api = BoltAPI()


class ArticleIn(Serializer):
    title: str
    body: str
    tags: list[str] = []

    @field_validator("title")
    def check_title(cls, value: str) -> str:
        if value.isupper():
            raise ValueError("Do not write the title in capitals")
        return value.strip()

    @field_validator("tags", mode="before")
    def split_tags(cls, value):
        if isinstance(value, str):
            return [tag.strip() for tag in value.split(",")]
        return value


@api.post("/articles")
async def create_article(data: ArticleIn) -> dict:
    return {"title": data.title, "tags": data.tags}
```

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"title": " Hello ", "body": "Text", "tags": "django, rust"}'
```

```json
{"title": "Hello", "tags": ["django", "rust"]}
```

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"title": "HELLO", "body": "Text"}'
```

```json
{"detail": [{"loc": ["body", "title"], "msg": "Do not write the title in capitals", "type": "value_error"}]}
```

The `mode` argument sets when the validator runs:

| `mode` | The validator gets | Bolt uses |
| --- | --- | --- |
| `"after"` (default) | The value after the type check | The return value. `None` keeps the value. |
| `"before"` | The raw input value, before the type check | The return value, also `None`. The type check then runs on it. |

A `"before"` validator runs only for a field that the input has. Bolt runs each `"before"` validator before each `"after"` validator. Other modes, such as `"wrap"`, raise `ValueError` when Python creates the class.

You can also write the method with `@classmethod` under `@field_validator`. A type checker then knows that the first argument is the class. A validator cannot be a `@staticmethod`.

## Validate several fields together

Add a method with `@model_validator`. The method gets the instance after the field validators ran. Raise `ValueError` or `TypeError` to reject the data:

```python
from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, model_validator

api = BoltAPI()


class PasswordChange(Serializer):
    password: str
    password_confirm: str

    @model_validator
    def passwords_match(self):
        if self.password != self.password_confirm:
            raise ValueError("The passwords do not match")


@api.post("/password")
async def change_password(data: PasswordChange) -> dict:
    return {"changed": True}
```

```json
{"detail": [{"loc": ["body"], "msg": "The passwords do not match", "type": "value_error"}]}
```

`@model_validator(mode="before")` makes a validator that gets the raw input, usually a dict, before any field validation. Write it as a `@classmethod`, and return the data to validate:

<!-- fragment -->
```python
    @model_validator(mode="before")
    @classmethod
    def accept_old_key(cls, data):
        if isinstance(data, dict) and "pass" in data:
            data = {**data, "password": data["pass"]}
        return data
```

The validators run in this order:

1. The model validators with `mode="before"`.
2. The field validators with `mode="before"`.
3. msgspec checks the types and the `Meta` constraints.
4. The field validators with `mode="after"`.
5. The model validators with `mode="after"`.

Steps 4 and 5 collect the errors of all validators. The response then has one entry for each error.
Bolt runs the validators that a parent class defines too. A subclass method with the same name replaces the parent validator.

## Validate data in Python code

Use `model_validate()` for a dict and `model_validate_json()` for JSON text. Both raise `RequestValidationError`. `model_validate()` reports each field that fails, not only the first one:

```python
from typing import Annotated

from msgspec import Meta

from django_bolt.exceptions import RequestValidationError
from django_bolt.serializers import Serializer


class AuthorIn(Serializer):
    username: Annotated[str, Meta(min_length=3)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+$")]
    age: Annotated[int, Meta(ge=0)]


try:
    AuthorIn.model_validate({"username": "al", "email": "nope", "age": -1})
except RequestValidationError as exc:
    for error in exc.errors():
        print(error)
```

```text
{'loc': ('body', 'username'), 'msg': 'Expected `str` of length >= 3', 'type': 'validation_error'}
{'loc': ('body', 'email'), 'msg': "Expected `str` matching regex '^[^@]+@[^@]+$'", 'type': 'validation_error'}
{'loc': ('body', 'age'), 'msg': 'Expected `int` >= 0', 'type': 'validation_error'}
```

A call of the class, such as `AuthorIn(username="x", email="y", age=-5)`, runs the validators of the serializer. It does not check the types or the `Meta` constraints. This is the behavior of msgspec.

## Configure a field with field()

Give a field the value `field(...)` to set its options. The `field()` value replaces the default value, so put the default in `field(default=...)`:

```python
from django_bolt.serializers import Serializer, field


class UserSerializer(Serializer):
    id: int = field(read_only=True, default=0)
    username: str
    email: str
    password: str = field(write_only=True, default="")


user = UserSerializer.model_validate({"id": 99, "username": "eve", "email": "eve@example.com", "password": "s3cret"})
print(user)
print(user.dump())
```

```text
UserSerializer(id=0, username='eve', email='eve@example.com', password='s3cret')
{'id': 0, 'username': 'eve', 'email': 'eve@example.com'}
```

The input could not set `id`, and the output does not show `password`.

| Argument | Default | Effect |
| --- | --- | --- |
| `read_only` | `False` | Input cannot set the field. The field gets its default, so it must have one. Python code and `from_model()` set it. |
| `write_only` | `False` | Input accepts the field. `dump()` and responses leave it out. |
| `source` | `None` | The attribute that `from_model()` reads. A dotted path such as `"author.email"` follows relations. |
| `alias` | `None` | The name of the field in JSON, for input and output. |
| `default` | no default | The default value. |
| `default_factory` | `None` | A function that makes the default, for example `list`. |
| `exclude` | `False` | `dump()` and responses always leave the field out. |
| `description` | `None` | The `description` of the property in the OpenAPI schema. |
| `title` | `None` | The `title` of the property in the OpenAPI schema. |
| `examples` | `None` | The `examples` of the property in the OpenAPI schema. |
| `deprecated` | `False` | Marks the property as deprecated in the OpenAPI schema. |
| `include_in_schema` | `True` | `False` leaves the property out of the OpenAPI schema. The field still validates input and is still in the output. |

`field()` has no constraint arguments. Use `Annotated[int, Meta(ge=0)]` for constraints.

A `read_only` field with no default raises `ImproperlyConfigured` at the first input:

```text
ImproperlyConfigured: UserSerializer.id is read_only and has no default. Input cannot set a read_only field. ...
```

With `forbid_unknown_fields=True` on the class, an input value for a `read_only` field is an error, not ignored.
This is also true for a field with a `msgspec.field()` default, and for a field of a base class.
For a field of a base class, the serializer declares the field again in the class that has the `Config`. The field keeps its type, default and JSON name.
Class creation raises `ImproperlyConfigured` when it cannot declare the field again:

- An annotation of the base class uses a name that is not defined yet.
- The type uses a type parameter with no type argument.
- The field is positional in the base class and keyword-only in the subclass, or the opposite.

In these cases, declare the field in the class that has the `Config`, with `field(read_only=True, default=...)`.

You can also list field names in the inner `Config` class: `read_only = {"id"}` and `write_only = {"password"}`. See [Config options](#config-options).

## Add a computed field

A method with `@computed_field` adds a field to the output. Its name is the name of the method. Its type is the return type:

```python
from django_bolt.serializers import Serializer, computed_field


class AuthorOut(Serializer):
    first_name: str
    last_name: str

    @computed_field
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @computed_field(alias="displayName")
    def display_name(self) -> str:
        return self.full_name().upper()


print(AuthorOut(first_name="Ada", last_name="Lovelace").dump())
```

```text
{'first_name': 'Ada', 'last_name': 'Lovelace', 'full_name': 'Ada Lovelace', 'displayName': 'ADA LOVELACE'}
```

A computed field is output only. Input does not accept it. Bolt calls the method at each `dump()`.
Inside the class, the computed field stays a method, so call it: `self.full_name()`.

The OpenAPI response schema has a required, read-only property for each computed field. `@computed_field` takes `alias`, `description`, `title`, `examples`, `deprecated` and `include_in_schema`.

## Nest serializers

A field with a `Serializer` type, or a `list` of one, is a nested serializer. Input validates each nested object:

```python
from django_bolt import BoltAPI
from django_bolt.serializers import Serializer

from blog.models import Article, Comment

api = BoltAPI()


class CommentIn(Serializer):
    author_name: str
    text: str


class ArticleIn(Serializer):
    title: str
    body: str
    author: int
    comments: list[CommentIn] = []


@api.post("/articles", status_code=201)
async def create_article(data: ArticleIn) -> dict:
    article = Article(title=data.title, body=data.body, author_id=data.author)
    await article.asave()
    await Comment.objects.abulk_create(
        [Comment(article=article, author_name=c.author_name, text=c.text) for c in data.comments]
    )
    return {"id": article.id, "comments": len(data.comments)}
```

```bash
curl -X POST localhost:8000/articles -H "Content-Type: application/json" \
  -d '{"title": "T", "body": "B", "author": 2, "comments": [{"author_name": "eve"}]}'
```

```json
{"detail": [{"loc": ["body", "comments", "0", "text"], "msg": "Object missing required field `text`", "type": "missing_field"}]}
```

For output, `from_model()` makes a nested serializer from the related object. The next section shows it.

## Build output from Django models

`from_model()` makes a serializer from a model instance. It reads each field from the attribute with the same name, or from `field(source=...)`.

A `ForeignKey` or `OneToOneField` field with the type `int` gets the primary key, with no query. A field with a nested `Serializer` type gets the related object. A `list` of a nested type gets the rows of a many-to-many or reverse relation.

### Return a QuerySet

A handler with the return type `list[SomeSerializer]` can return a QuerySet. Bolt then loads what the serializer reads, with a bounded number of queries:

```python
from django.db.models import Count

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, computed_field, field

from blog.models import Article

api = BoltAPI()


class AuthorOut(Serializer):
    id: int
    username: str


class TagOut(Serializer):
    name: str


class ArticleOut(Serializer):
    id: int
    title: str
    author: AuthorOut
    tags: list[TagOut]
    author_email: str = field(source="author.email")
    comment_count: int = 0

    class Config:
        annotations = {"comment_count": Count("comments")}

    @computed_field
    def summary(self) -> str:
        return f"{self.title} by {self.author.username}"


@api.get("/articles")
async def list_articles() -> list[ArticleOut]:
    return Article.objects.filter(published=True).order_by("id")
```

```bash
curl localhost:8000/articles
```

```json
[
  {"id": 1, "title": "Hello Bolt", "author": {"id": 1, "username": "alice"}, "tags": [{"name": "django"}, {"name": "rust"}], "author_email": "alice@example.com", "comment_count": 2, "summary": "Hello Bolt by alice"},
  {"id": 2, "title": "Async ORM", "author": {"id": 1, "username": "alice"}, "tags": [{"name": "django"}], "author_email": "alice@example.com", "comment_count": 0, "summary": "Async ORM by alice"}
]
```

Bolt reads the fields of the serializer and makes a loading plan for the QuerySet:

- A `ForeignKey` or `OneToOneField` with a nested serializer, or a dotted `source`, adds `select_related()`.
- A many-to-many or reverse relation adds `prefetch_related()`. A nested serializer adds its own plan to the prefetch.
- `Config.annotations` adds `annotate()`.
- On Django 6.1 and later, Bolt also sets `fetch_mode(FETCH_PEERS)`. A relation that the plan does not name then loads for all rows in one query.

The plan only adds loads. Bolt keeps each `select_related()`, `prefetch_related()` and `annotate()` that you applied. `ArticleOut.loading_plan(Article)` shows the plan:

```text
LoadingPlan(select_related=('author',), prefetch=('tags',), annotations={'comment_count': Count(F(comments))})
```

The same plan applies to a QuerySet that a [paginated](pagination.md) handler returns. In your own code, call `ArticleOut.from_models(queryset)` in sync code and `await ArticleOut.afrom_models(queryset)` in async code. Both return a list of serializer instances. The example list above takes two queries with `from_models()`.

### Convert one instance

`from_model()` does not run queries. It reads only the relations that the instance already has. A required nested field with an unloaded relation raises `UnloadedRelationError`:

```text
UnloadedRelationError: ArticleOut.author requires loaded relation 'author' on Article when using from_model(). Preload it with select_related('author') or use await ArticleOut.afrom_model(instance).
```

A nested field with a default keeps its default when the relation is not loaded.

Use one of these ways for a single instance:

- Load the relations first: `await Article.objects.select_related("author").prefetch_related("tags").aget(id=1)`, then call `from_model()`.
- Call `await ArticleOut.afrom_model(article)` in async code. It loads each missing relation with the async ORM.
- Return the instance from a handler with the return type `ArticleOut`. Bolt tries `from_model()`, and in an async handler it falls back to `afrom_model()`.

`Config.annotations` applies to a QuerySet only. For one instance, annotate the QuerySet that loads it.

`from_model()` and `afrom_model()` take `max_depth` (default `10`). A deeper chain of nested serializers raises `ValueError`.

## Write to Django models

`to_model(Model)` makes an unsaved model instance. `update_instance(instance)` sets the fields of an existing instance and does not save it. Both skip `read_only` fields:

```python
from django_bolt import BoltAPI
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class ArticleUpdate(Serializer):
    title: str
    body: str


@api.put("/articles/{article_id}")
async def replace_article(article_id: int, data: ArticleUpdate) -> dict:
    article = await Article.objects.aget(id=article_id)
    data.update_instance(article)
    await article.asave()
    return {"id": article.id, "title": article.title}
```

A `ForeignKey` or `OneToOneField` field can hold the primary key (`author: int`) or a model instance. Bolt writes a primary key to the column attribute, `author_id`.

`to_dict()` returns the fields as a dict, with no `dump()` rules: `write_only` fields stay in, and computed fields are not added.

## Accept a partial update

Give each field the type `X | UnsetType` and the default `UNSET`. A field that the request does not send then holds `UNSET`. `to_dict(exclude_unset=True)` leaves those fields out:

```python
from msgspec import UNSET, UnsetType

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer

from blog.models import Article

api = BoltAPI()


class AuthorOut(Serializer):
    id: int
    username: str


class ArticleOut(Serializer):
    id: int
    title: str
    author: AuthorOut


class ArticlePatch(Serializer):
    title: str | UnsetType = UNSET
    body: str | UnsetType = UNSET
    published: bool | UnsetType = UNSET


@api.patch("/articles/{article_id}")
async def update_article(article_id: int, data: ArticlePatch) -> ArticleOut:
    article = await Article.objects.select_related("author").aget(id=article_id)
    changes = data.to_dict(exclude_unset=True)
    for name, value in changes.items():
        setattr(article, name, value)
    await article.asave(update_fields=list(changes))
    return article
```

```bash
curl -X PATCH localhost:8000/articles/3 -H "Content-Type: application/json" -d '{"published": true}'
```

```json
{"id": 3, "title": "Draft", "author": {"id": 2, "username": "bob"}}
```

`dump()` also takes `exclude_unset=True`.

## Choose the output fields

One serializer can give several output shapes. List the shapes in `Config.field_sets`:

```python
from django.contrib.auth.models import User

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, computed_field, field

api = BoltAPI()


class UserSerializer(Serializer):
    id: int
    username: str
    email: str
    password: str = field(write_only=True, default="")
    is_staff: bool = False

    class Config:
        field_sets = {
            "list": ["id", "username"],
            "detail": ["id", "username", "email", "is_staff"],
        }

    @computed_field
    def handle(self) -> str:
        return f"@{self.username}"


UserListItem = UserSerializer.fields("list")
UserDetail = UserSerializer.subset("id", "username", "email", "handle", name="UserDetail")


@api.get("/users")
async def list_users() -> list[UserListItem]:
    return User.objects.order_by("id")


@api.get("/users/{user_id}")
async def get_user(user_id: int) -> UserDetail:
    return await User.objects.aget(id=user_id)
```

```bash
curl localhost:8000/users
```

```json
[{"id": 1, "username": "alice"}, {"id": 2, "username": "bob"}]
```

```bash
curl localhost:8000/users/1
```

```json
{"id": 1, "username": "alice", "email": "alice@example.com", "handle": "@alice"}
```

There are two kinds of tools:

- `subset(*fields, name=None)` and `fields(field_set, name=None)` make a new `Serializer` class. Use the class as a type: a response type, a body type or a `list[...]` item. Create these classes at module level. The new class keeps the validators of its fields, and it has `from_parent(instance)` to copy an instance of the full class.
- `only(*fields)`, `exclude(*fields)` and `use(field_set)` return a view for output in Python code. A view has `dump()`, `dump_many()`, `dump_json()`, `dump_many_json()`, `from_model()` and `afrom_model()`. A view is not a type.

```python
user = UserSerializer(id=1, username="alice", email="alice@example.com", password="pw")

print(UserSerializer.only("id", "email").dump(user))
print(UserSerializer.exclude("email", "handle").dump(user))
print(UserSerializer.use("list").dump(user))
```

```text
{'id': 1, 'email': 'alice@example.com'}
{'id': 1, 'username': 'alice', 'is_staff': False}
{'id': 1, 'username': 'alice'}
```

A computed field is in a field set only when the set names it.

## Leave fields out of the output

`dump()`, `dump_json()`, `dump_many()` and `dump_many_json()` take these keyword arguments:

| Argument | Default | Effect |
| --- | --- | --- |
| `exclude_none` | `False` | Leave out each field with the value `None`. |
| `exclude_unset` | `False` | Leave out each field with the value `msgspec.UNSET`. |
| `exclude_defaults` | `False` | Leave out each field with its default value. |
| `by_alias` | `False` | No effect. The output always uses the alias. |

A handler that returns a serializer gets the output of `dump()` with no arguments.

## Rename fields in JSON

Pass the msgspec `rename` option to the class to rename all fields. `field(alias=...)` renames one field and wins over `rename`:

```python
from django_bolt.serializers import Serializer, field


class ArticleMeta(Serializer, rename="camel"):
    created_by: str
    is_draft: bool = field(alias="draft", default=False)


print(ArticleMeta.model_validate({"createdBy": "alice", "draft": True}).dump())
```

```text
{'createdBy': 'alice', 'draft': True}
```

Input and output both use the JSON names. Python code uses the field names. Other msgspec class options, such as `forbid_unknown_fields=True`, `omit_defaults=True` and `frozen=True`, also work.

## Create a serializer from a model

`create_serializer()` makes a `Serializer` class from the fields of a model:

```python
from django_bolt.serializers import create_serializer, create_serializer_set

from blog.models import Article

ArticleSerializer = create_serializer(
    Article,
    fields=["id", "title", "author", "published", "created_at"],
    read_only={"id", "created_at"},
)

ArticleCreate, ArticleUpdate, ArticlePublic = create_serializer_set(
    Article,
    create_fields=["title", "body", "author"],
    update_fields=["title", "body"],
    public_fields=["id", "title", "author", "created_at"],
)
```

`create_serializer()` takes `fields`, `exclude`, `read_only`, `write_only` and `serializer_name`. The field order of the class is the order of `fields`.
The fields of the class have no defaults. Thus a class with `read_only` fields is for output only: input raises `ImproperlyConfigured`. `create_serializer_set()` returns three classes: `<Model>Create`, `<Model>Update` and `<Model>Public`. Each field of `update_fields` is required.

The model fields get these types:

| Model field | Serializer type |
| --- | --- |
| `CharField`, `EmailField`, `SlugField`, `URLField` | `str` with `max_length` |
| `CharField` or `IntegerField` with `choices` | `Literal[...]` of the choice values |
| `TextField` | `str` |
| `IntegerField` and its subclasses | `int` |
| `FloatField` | `float` |
| `DecimalField` | `Decimal`. JSON output is a string, for example `"19.99"`. |
| `BooleanField` | `bool` |
| `DateTimeField`, `DateField`, `TimeField` | `datetime`, `date`, `time` |
| `DurationField` | `str` |
| `UUIDField` | `UUID` |
| `ForeignKey`, `OneToOneField` | `int`, the primary key |
| A field with `null=True` | the type above `\| None` |

`create_serializer()` skips `ManyToManyField` fields. Write a nested field by hand for them.

## Use the built-in field types

`django_bolt.serializers` exports `Annotated` types with common constraints:

| Type | Base | Constraint |
| --- | --- | --- |
| `Char50`, `Char100`, `Char150`, `Char200`, `Char255`, `Char500`, `Char1000` | `str` | `max_length` of the number |
| `Text`, `NonEmptyStr` | `str` | none; `min_length=1` |
| `Email` | `str` | `max_length=254` and an email pattern |
| `URL`, `HttpsURL` | `str` | `max_length=200`, `http(s)://` or `https://` |
| `Slug`, `Slug100`, `Slug200` | `str` | letters, digits, `-` and `_`; `max_length` 50, 100, 200 |
| `UUID` | `str` | the pattern of a UUID |
| `Username` | `str` | `max_length=150`, the characters of Django usernames |
| `Password` | `str` | `min_length=8`, `max_length=128` |
| `Phone` | `str` | E.164 format, `max_length=20` |
| `HexColor` | `str` | `#` and six hex digits |
| `CurrencyCode`, `CountryCode`, `LanguageCode` | `str` | `USD`, `US`, `en` or `en-US` |
| `Timezone` | `str` | `Area/City` or `UTC` |
| `IPv4`, `IPv6`, `IP` | `str` | the pattern of the address |
| `FilePath` | `str` | `max_length=100` |
| `SmallInt`, `Int`, `BigInt` | `int` | the range of the database column |
| `PositiveSmallInt`, `PositiveBigInt`, `NonNegativeInt` | `int` | `ge=0`, with the column maximum |
| `PositiveInt` | `int` | `gt=0` |
| `Port`, `HttpStatus` | `int` | 1 to 65535; 100 to 599 |
| `Float`, `PositiveFloat` | `float` | none; `ge=0` |
| `Latitude`, `Longitude` | `float` | -90 to 90; -180 to 180 |
| `Percentage`, `Rating`, `Rating10` | `float` | 0 to 100; 0 to 5; 0 to 10 |

`django_bolt.serializers` also exports `Meta` from msgspec.

## Config options

The inner `Config` class of a serializer has these options:

| Option | Type | Effect |
| --- | --- | --- |
| `read_only` | `set[str]` | Field names that input cannot set. Each field must have a default. |
| `write_only` | `set[str]` | Field names that `dump()` and responses leave out. |
| `field_sets` | `dict[str, list[str]]` | Named lists of fields for `use()` and `fields()`. |
| `annotations` | `dict[str, Expression]` | Query annotations that the loading plan adds to a QuerySet. |
| `model` | model class | The model of the serializer. `create_serializer()` sets it. Bolt does not read it. |

`Config` has no `validators` option. A `Config` with `validators` raises `TypeError` when Python creates the class.

## See also

- [Serializers reference](../ref/serializers.md)
- [Async ORM](async-orm.md)
- [Pagination](pagination.md)
- [Requests](requests.md)
- [Responses](responses.md)

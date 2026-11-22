---
description: Reference for the parameter markers in django_bolt.params - Query, Path, Body, Header, Cookie, Form, File and Depends - and for UploadFile.
---

# Parameters

A marker sets the source of a handler argument and adds limits and documentation.
Put the marker in `Annotated`, or use it as the default of the argument.
For a task-based introduction, see [Requests](../topics/requests.md).

```python
from django_bolt.params import Body, Cookie, Depends, File, Form, Header, Path, Query
```

`django_bolt.param_functions` has the same functions. `django_bolt.Depends` is the same class as `django_bolt.params.Depends`.

**Example**

```python
from typing import Annotated

from django_bolt import BoltAPI
from django_bolt.params import Query

api = BoltAPI()


@api.get("/search")
async def search(
    q: Annotated[str, Query(min_length=2, max_length=50, description="Text to find", example="django")],
    sort: Annotated[str, Query(pattern=r"^-?(created_at|title)$")] = "-created_at",
) -> dict:
    return {"q": q, "sort": sort}
```

## Rules for each marker

- The first argument of a marker is `default`. `Query("x")` sets a default, not an alias.
- `...` as the default makes the argument required.
- In `Annotated`, write the default after `=`. A `default` in the marker raises `TypeError` when you register the route.
- As the default of the argument, for example `page: int = Query(1, ge=1)`, the `default` of the marker is the default of the argument.
- The OpenAPI document shows `description`, `example` and `deprecated`.

## Query()

<!-- fragment -->
```python
Query(default=..., *, alias=None, gt=None, ge=None, lt=None, le=None, min_length=None,
      max_length=None, pattern=None, description=None, example=None, deprecated=False)
```

Read the argument from the query string.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `default` | `Any` | `...` | The value when the key is missing. `...` makes the key required. |
| `alias` | `str \| None` | `None` | The key to read, in place of the argument name. |
| `gt` | `float \| None` | `None` | The value must be greater than this number. |
| `ge` | `float \| None` | `None` | The value must be greater than or equal to this number. |
| `lt` | `float \| None` | `None` | The value must be less than this number. |
| `le` | `float \| None` | `None` | The value must be less than or equal to this number. |
| `min_length` | `int \| None` | `None` | The minimum length of a string or a list. |
| `max_length` | `int \| None` | `None` | The maximum length of a string or a list. |
| `pattern` | `str \| None` | `None` | A regular expression that a string must match. |
| `description` | `str \| None` | `None` | The description in the OpenAPI document. |
| `example` | `Any` | `None` | The example in the OpenAPI document. |
| `deprecated` | `bool` | `False` | Mark the parameter as deprecated in the OpenAPI document. |

A value outside a limit gets a 422 response. A `msgspec.Struct` type reads each of its fields from the query string. See [Group parameters in a model](../topics/requests.md#group-parameters-in-a-model).

## Path()

<!-- fragment -->
```python
Path(default=..., *, alias=None, gt=None, ge=None, lt=None, le=None, min_length=None,
     max_length=None, pattern=None, description=None, example=None, deprecated=False)
```

Read the argument from the path. An argument with the name of a path parameter needs no marker. Use `Path()` to add limits.
The parameters are the parameters of `Query()`.

**Errors**

- `ValueError`: `default` is not `...`. A path parameter is always required.

```python
from django_bolt.params import Path


@api.get("/articles/{article_id}")
async def get_article(article_id: Annotated[int, Path(ge=1)]) -> dict:
    return {"id": article_id}
```

## Body()

<!-- fragment -->
```python
Body(default=..., *, alias=None, embed=False, description=None, example=None)
```

Read the argument from the JSON body.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `default` | `Any` | `...` | The value when the request has no body. With `embed=True`, also the value when the object has no key for the argument. |
| `alias` | `str \| None` | `None` | With `embed=True`, the key to read, in place of the argument name. Without `embed`, it has no effect. |
| `embed` | `bool` | `False` | Read the value at the key of the argument name in a JSON object. A body that is not an object gets a 422 response. |
| `description` | `str \| None` | `None` | The description of the request body in the OpenAPI document. |
| `example` | `Any` | `None` | The example of the request body in the OpenAPI document. |

A `GET`, `HEAD`, `DELETE` or `OPTIONS` route with a `Body()` argument raises `TypeError` when you register it.

## Header()

<!-- fragment -->
```python
Header(default=..., *, alias=None, description=None, example=None, deprecated=False)
```

Read the argument from a request header. Without `alias`, the header name is the argument name in lower case, with `-` for each `_`. The match is not case-sensitive.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `default` | `Any` | `...` | The value when the header is missing. |
| `alias` | `str \| None` | `None` | The header name. |
| `description` | `str \| None` | `None` | The description in the OpenAPI document. |
| `example` | `Any` | `None` | The example in the OpenAPI document. |
| `deprecated` | `bool` | `False` | Mark the parameter as deprecated in the OpenAPI document. |

To add a limit, put `msgspec.Meta` in `Annotated`, for example `Annotated[int, Header(), msgspec.Meta(le=100)]`.

## Cookie()

<!-- fragment -->
```python
Cookie(default=..., *, alias=None, description=None, example=None, deprecated=False)
```

Read the argument from a request cookie. Without `alias`, the cookie name is the argument name.
The parameters are the parameters of `Header()`.

This marker reads a cookie. To set a cookie on a response, see [Set and delete cookies](../topics/responses.md#set-and-delete-cookies).

## Form()

<!-- fragment -->
```python
Form(default=..., *, alias=None, description=None, example=None)
```

Read the argument from a URL-encoded or multipart form body.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `default` | `Any` | `...` | The value when the field is missing. |
| `alias` | `str \| None` | `None` | The field name. |
| `description` | `str \| None` | `None` | The description of the field in the OpenAPI document. |
| `example` | `Any` | `None` | The example of the field in the OpenAPI document. |

A `msgspec.Struct` type reads each of its fields from the form. A field can be an `UploadFile`.

## File()

<!-- fragment -->
```python
File(default=..., *, alias=None, description=None, max_size=None, min_size=None,
     allowed_types=None, max_files=None)
```

Read the argument from a file field of a multipart body. The type of the argument can be `UploadFile`, `bytes`, or a `list` of one of them.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `default` | `Any` | `...` | The value when the field is missing. |
| `alias` | `str \| None` | `None` | The field name. |
| `description` | `str \| None` | `None` | The description in the OpenAPI document. |
| `max_size` | `int \| None` | `None` | The maximum size of each file, in bytes. An `int` of 0 or more. |
| `min_size` | `int \| None` | `None` | The minimum size of each file, in bytes. An `int` of 0 or more, not above `max_size`. |
| `allowed_types` | `Iterable[str] \| None` | `None` | The allowed content types. `"image/*"` matches each image type. `None` allows each type. |
| `max_files` | `int \| None` | `None` | The maximum number of files for a `list` argument. An `int` of 1 or more. |

**Errors**

- `TypeError`: a limit is not an `int`, for example `max_size="10MB"`. A `bool` is not an `int` here.
- `TypeError`: `allowed_types` is a `str`, or has an item that is not a `str`.
- `ValueError`: a limit is out of range, `min_size` is above `max_size`, or `allowed_types` is empty.

These errors occur when you call `File()`.
A file that fails a check gets a 422 response with the type `file_missing`, `file_too_large`, `file_too_small`, `file_invalid_content_type` or `file_too_many`.
The `BOLT_MAX_UPLOAD_SIZE` setting limits the whole body before these checks. See the [settings reference](settings.md).

```python
from django_bolt import FileSize, UploadFile
from django_bolt.params import File


@api.post("/articles/{article_id}/cover")
async def upload_cover(
    article_id: int,
    cover: Annotated[UploadFile, File(max_size=FileSize.MB_2, allowed_types=["image/png", "image/jpeg"])],
) -> dict:
    return {"article_id": article_id, "filename": cover.filename, "size": cover.size}
```

### FileSize

`django_bolt.FileSize` is an `IntEnum` of sizes in bytes, for `max_size` and `min_size`.
The members are `MB_1` to `MB_10` in steps of 1, and `MB_20` to `MB_100` in steps of 10. One `MB` is 1,048,576 bytes.

## Depends()

<!-- fragment -->
```python
Depends(dependency=None, use_cache=True)
```

Give the argument the return value of a function. The function can take request values as a handler does.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `dependency` | `Callable \| None` | `None` | The function to call. It can be sync or async. `Depends()` with no function raises `TypeError` when you register the route. |
| `use_cache` | `bool` | `True` | Call the function one time for each request, also when more than one argument needs it. `False` calls it for each argument. |

```python
import msgspec

from django_bolt import Depends


class Pagination(msgspec.Struct):
    page: int
    page_size: int


async def pagination(page: int = 1, page_size: Annotated[int, Query(le=100)] = 20) -> Pagination:
    return Pagination(page=page, page_size=page_size)


@api.get("/articles")
async def list_articles(paging: Annotated[Pagination, Depends(pagination)]) -> dict:
    return {"page": paging.page, "page_size": paging.page_size}
```

See [Dependencies](../topics/dependencies.md).

## Param

`Param` is the frozen dataclass that each marker function returns. Its fields are `source`, `default`, the arguments of the marker functions, and `multiple_of`.
You do not need to make a `Param` yourself.

## UploadFile

<!-- fragment -->
```python
from django_bolt import UploadFile
```

The value of an `UploadFile` argument. Bolt makes it. Do not make one yourself.

| Attribute | Type | Description |
| --- | --- | --- |
| `filename` | `str` | The file name that the client sent. |
| `content_type` | `str` | The content type of the part. The default is `"application/octet-stream"`. |
| `size` | `int` | The size in bytes. |
| `headers` | `dict[str, str]` | The headers of the multipart part. |
| `file` | `django.core.files.File` | A Django `File` of the content. Assign it to a `FileField` or an `ImageField`. |
| `raw_file` | file object | The file object that holds the content. |
| `rolled_to_disk` | `bool` | `True` when the content is in a temporary file. |

| Method | Description |
| --- | --- |
| `await read(size=-1)` | Read `size` bytes, or all the bytes for `-1`. |
| `await seek(offset, whence=0)` | Move the position. Return the new position. |
| `await write(data)` | Write bytes. Return the number of bytes. |
| `await close()` | Close the file. |

A file larger than the `BOLT_MEMORY_SPOOL_THRESHOLD` setting is in a temporary file. The async methods then run the file operation in a thread.
Bolt closes each `UploadFile` and removes its temporary file after the response.

## See also

- [Requests](../topics/requests.md)
- [Request reference](request.md)
- [Dependencies](../topics/dependencies.md)
- [Settings reference](settings.md)

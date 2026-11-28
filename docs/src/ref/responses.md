---
description: Reference for the response classes in django_bolt.responses, the cookie methods, the Cookie class and the render() shortcut.
---

# Responses

A handler returns one of these classes to set the content type, the status, the headers or the cookies.
For a task-based introduction, see [Responses](../topics/responses.md).

```python
from django_bolt import JSON, EventSourceResponse, Response, ServerSentEvent, StreamingResponse
from django_bolt.responses import HTML, File, FileResponse, PlainText, Redirect
```

**Example**

```python
from django_bolt import BoltAPI, JSON

api = BoltAPI()


@api.post("/articles")
async def create_article():
    return JSON({"id": 1}, status_code=201, headers={"Location": "/articles/1"}).set_cookie("last_article", "1")
```

A handler with `-> JSON` or `-> Response` sends the body as it is. Bolt does not validate it. `-> Item | JSON` validates the body against `Item`. `Annotated` and a `type` alias do not change this.

## Shared behavior

- `status_code` must be from 200 to 599. Another status gets a 500 response.
- Each `headers` value must be a `str`. Another type gets a 500 response.
- Each class has the methods [`set_cookie()`](#set_cookie) and [`delete_cookie()`](#delete_cookie).
- The constructor arguments are attributes of the object with the same names, for example `response.status_code`.

## Response

<!-- fragment -->
```python
Response(content=None, status_code=200, headers=None, media_type="application/json")
```

A response with any content type.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `content` | `Any` | `None` | The body. `None` is `{}`. |
| `status_code` | `int` | `200` | The status. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |
| `media_type` | `str` | `"application/json"` | The `Content-Type` header. |

`bytes`, `bytearray` and `memoryview` content goes to the client with no change.
With the media type `application/json`, other content is encoded as JSON.
With another media type, a `str` is encoded as UTF-8, and another value is encoded as the UTF-8 of `str(content)`.

## JSON

<!-- fragment -->
```python
JSON(data, status_code=200, headers=None)
```

A JSON response. The content type is `application/json`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `data` | `Any` | (required) | The value to encode as JSON. |
| `status_code` | `int` | `200` | The status. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |

## PlainText

<!-- fragment -->
```python
PlainText(text, status_code=200, headers=None)
```

A text response. The content type is `text/plain; charset=utf-8`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `text` | `str` | (required) | The body. |
| `status_code` | `int` | `200` | The status. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |

## HTML

<!-- fragment -->
```python
HTML(html, status_code=200, headers=None)
```

An HTML response. The content type is `text/html; charset=utf-8`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `html` | `str` | (required) | The body. |
| `status_code` | `int` | `200` | The status. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |

## Redirect

<!-- fragment -->
```python
Redirect(url, status_code=307, headers=None)
```

A redirect with a `Location` header and no body.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | `str` | (required) | The `Location` header. |
| `status_code` | `int` | `307` | The status. Use `301`, `302`, `303`, `307` or `308`. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |

## File

<!-- fragment -->
```python
File(path, *, media_type=None, filename=None, status_code=200, headers=None)
```

A file response. Bolt reads the full file into memory. For a large file, use `FileResponse`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | `str` | (required) | The path of the file. |
| `media_type` | `str \| None` | `None` | The content type. `None` guesses it from the file name. |
| `filename` | `str \| None` | `None` | Adds `Content-Disposition: attachment` with this file name. |
| `status_code` | `int` | `200` | The status. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |

**Errors**

- A path that names no regular file gets 404 `{"detail": "File not found"}`.

`File` does not check `BOLT_ALLOWED_FILE_PATHS`.

## FileResponse

<!-- fragment -->
```python
FileResponse(path, *, media_type=None, filename=None, status_code=200, headers=None)
```

A file response that Bolt streams from disk. Python does not read the file.
The parameters are the parameters of `File`.

**Errors**

The constructor raises these exceptions. Bolt answers them, and the response does not show the path.

- `ResponseFileNotFound`: the path names no regular file, for example a missing file or a directory. The response is 404 `{"detail": "File not found"}`.
- `ResponseFileNotAllowed`: `BOLT_ALLOWED_FILE_PATHS` is set and the path is outside its directories. The response is 403 `{"detail": "Permission denied"}`.
- `ImproperlyConfigured`: `BOLT_ALLOWED_FILE_PATHS` is not a list of absolute paths.

`BOLT_ALLOWED_FILE_PATHS` is a list of absolute directories. `None` or an empty list allows each path.
`runbolt` reads it at startup, so a wrong value stops the server.

## StreamingResponse

<!-- fragment -->
```python
StreamingResponse(content, *, status_code=200, media_type=None, headers=None)
```

A response that Bolt sends in chunks, one for each item of `content`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `content` | generator or iterator | (required) | A generator, an async generator, or an iterator. Each item is `str` or `bytes`. |
| `status_code` | `int` | `200` | The status. |
| `media_type` | `str \| None` | `None` | The content type. `None` is `application/octet-stream`. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |

**Errors**

- `TypeError`: `content` is a function, for example a generator function with no call, or is not an iterator.

Bolt sends the status and the headers before the first chunk. An exception in the generator ends the body. Bolt logs it to the `django_bolt.streaming` logger.

## EventSourceResponse

<!-- fragment -->
```python
EventSourceResponse(content, *, status_code=200, headers=None, ping_interval=15.0)
```

A `StreamingResponse` for Server-Sent Events. The content type is `text/event-stream`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `content` | generator or iterator | (required) | The events. An item can be a `ServerSentEvent`, or a value that Bolt sends as the JSON `data` of an event. |
| `status_code` | `int` | `200` | The status. |
| `headers` | `dict[str, str] \| None` | `None` | More response headers. |
| `ping_interval` | `float \| None` | `15.0` | Seconds with no event before Bolt sends a keep-alive comment. |

You can also set `response_class=EventSourceResponse` on the route decorator of a generator handler. See [Server-Sent Events](../topics/sse.md).

## ServerSentEvent

<!-- fragment -->
```python
ServerSentEvent(data=None, raw_data=None, event=None, id=None, retry=None, comment=None)
```

One event of an `EventSourceResponse`. It is a frozen `msgspec.Struct`.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `data` | `Any` | `None` | A value that Bolt encodes as JSON in the `data:` lines. |
| `raw_data` | `str \| None` | `None` | Text for the `data:` lines, with no encoding. |
| `event` | `str \| None` | `None` | The event name. |
| `id` | `str \| None` | `None` | The event ID. |
| `retry` | `int \| None` | `None` | The reconnection time of the client, in milliseconds. |
| `comment` | `str \| None` | `None` | A comment line. |

**Errors**

- `ValueError`: both `data` and `raw_data` are set, `id` has a null character, or `retry` is negative.

## format_sse_event()

<!-- fragment -->
```python
format_sse_event(*, data_str=None, data_bytes=None, event=None, id=None, retry=None, comment=None) -> bytes
```

Return the wire form of one event. The result ends with a blank line. Give `data_bytes` for data that is already encoded.
A browser reads the data back unchanged; only CR, LF and CRLF split it into `data:` lines. A line break in `id` or `event` raises `ValueError`.

## set_cookie()

<!-- fragment -->
```python
response.set_cookie(name, value="", max_age=None, expires=None, path="/", domain=None,
                    secure=False, httponly=False, samesite="Lax") -> response
```

Add a `Set-Cookie` header. Return the response, so that you can chain calls.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | (required) | The cookie name. |
| `value` | `str` | `""` | The cookie value. |
| `max_age` | `int \| None` | `None` | The age in seconds. `None` makes a session cookie. |
| `expires` | `datetime \| str \| None` | `None` | The expiry date. Bolt sends an aware `datetime` in UTC, as Django does. A naive `datetime` is UTC. |
| `path` | `str` | `"/"` | The path of the cookie. |
| `domain` | `str \| None` | `None` | The domain of the cookie. `None` is the domain of the request. |
| `secure` | `bool` | `False` | Send the cookie only over HTTPS. A browser keeps a `SameSite=None` cookie only with `secure=True`. |
| `httponly` | `bool` | `False` | Hide the cookie from JavaScript. |
| `samesite` | `"Strict" \| "Lax" \| "None" \| False` | `"Lax"` | The `SameSite` attribute. `False` leaves it out. The letter case has no meaning. |

**Errors**

- `ValueError`: `samesite` has another value.

Bolt does not send a cookie with a control character or a character above U+00FF in its value, and logs a warning.
Bolt also does not send a cookie with a `;` or a control character in its `path`, `domain` or `expires`, and logs a warning. A `;` would add an attribute.
Bolt sends `Secure` only for `secure=True`, as Django does. Thus `samesite="None"` alone gives no `Secure`.

## delete_cookie()

<!-- fragment -->
```python
response.delete_cookie(name, path="/", domain=None) -> response
```

Send the cookie with an empty value, `Max-Age=0` and an expiry date in 1970. Return the response.
Give the `path` and `domain` of the cookie. A name that starts with `__Secure-` or `__Host-` gets the `Secure` attribute.

## Cookie

<!-- fragment -->
```python
from django_bolt import Cookie

Cookie(name, value="", max_age=None, expires=None, path="/", domain=None,
       secure=False, httponly=False, samesite="Lax")
```

A response cookie, with the fields of `set_cookie()`. `cookie.to_header_value()` returns the `Set-Cookie` value.
This class is not the `Cookie()` marker of `django_bolt.params`, which reads a request cookie.

## render()

<!-- fragment -->
```python
from django_bolt.shortcuts import render

render(request, template_name, context=None, content_type="text/html; charset=utf-8", status=200, using=None) -> Response
```

Render a Django template to a `Response`. The arguments have the meaning that they have in `django.shortcuts.render()`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `request` | `Request` | (required) | The request. Context processors read it. |
| `template_name` | `str` | (required) | The template name. |
| `context` | `dict \| None` | `None` | The template context. |
| `content_type` | `str` | `"text/html; charset=utf-8"` | The `Content-Type` header. |
| `status` | `int` | `200` | The status. |
| `using` | `str \| None` | `None` | The name of the template engine. |

## See also

- [Responses](../topics/responses.md)
- [Server-Sent Events](../topics/sse.md)
- [Exceptions reference](exceptions.md)
- [Settings reference](settings.md)

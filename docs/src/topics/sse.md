---
description: Send Server-Sent Events and other streaming responses from async or sync generators, with keep-alive pings and cleanup on disconnect.
---

# Server-Sent Events and streaming

A streaming response sends its body in parts while the handler runs.
Use Server-Sent Events (SSE) to push updates to a browser over one HTTP response.
Use a plain stream for a large export, for example CSV or JSON lines.

Write the handler as a generator and set `response_class=EventSourceResponse`:

```python title="blog/api.py"
import asyncio
from collections.abc import AsyncIterator

from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse

api = BoltAPI()


@api.get("/articles/{article_id}/live", response_class=EventSourceResponse)
async def article_live(article_id: int) -> AsyncIterator[dict]:
    for readers in range(1, 4):
        yield {"article_id": article_id, "readers": readers}
        await asyncio.sleep(1)
```

```bash
curl -N localhost:8000/articles/7/live
```

```text
data: {"article_id":7,"readers":1}

data: {"article_id":7,"readers":2}

data: {"article_id":7,"readers":3}

```

Each event arrives one second after the previous event.
Bolt encodes each yielded object as JSON and puts it in a `data:` line.
The response ends when the generator ends.

An SSE response has these headers:

```text
content-type: text/event-stream
cache-control: no-cache, no-store, must-revalidate
x-accel-buffering: no
pragma: no-cache
expires: 0
```

`x-accel-buffering: no` tells nginx to send each event at once.

## Read the events in a browser

Use the browser `EventSource` API:

```javascript
const source = new EventSource("/articles/7/live");
source.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(data.readers);
};
```

When the response ends, the browser connects again after a short time.
To stop, call `source.close()`.

## Set the event name, id and retry time

Yield a `ServerSentEvent` to set the other SSE fields:

```python
from django_bolt.responses import ServerSentEvent


@api.get("/articles/feed", response_class=EventSourceResponse)
async def article_feed() -> AsyncIterator[ServerSentEvent]:
    yield ServerSentEvent(retry=10000)
    yield ServerSentEvent(data={"id": 1, "title": "Hello Bolt"}, event="published", id="1")
    yield ServerSentEvent(data={"id": 1}, event="deleted", id="2")
```

```text
retry: 10000

event: published
data: {"id":1,"title":"Hello Bolt"}
id: 1

event: deleted
data: {"id":1}
id: 2

```

In the browser, listen for a named event with `source.addEventListener("published", ...)`.
`onmessage` gets only the events that have no name.

| Field | Type | Default | Wire format |
| --- | --- | --- | --- |
| `data` | any JSON value | `None` | `data:` line with the JSON of the value |
| `raw_data` | `str \| None` | `None` | `data:` line with the text as it is |
| `event` | `str \| None` | `None` | `event:` line |
| `id` | `str \| None` | `None` | `id:` line. The browser sends it back in `Last-Event-ID` when it connects again. |
| `retry` | `int \| None` | `None` | `retry:` line. The time in milliseconds that the browser waits before it connects again. |
| `comment` | `str \| None` | `None` | `:` line. The browser ignores it. |

Text with line breaks becomes one `data:` line for each line. Only CR, LF and CRLF end a line, as in the browser.
Thus U+2028 and the other Unicode line separators stay in the data, and a browser reads the data back unchanged.
A browser joins the lines with LF, so a CR or CRLF in the data arrives as LF.
Empty data, and data that ends with a line break, read back unchanged too.
`ServerSentEvent` raises `ValueError` in these cases:

- You set both `data` and `raw_data`.
- `id` contains a null character.
- `id` or `event` contains a line break (CR or LF). A line break would end the field, and the text after it would become another field or another event.
- `retry` is less than 0.

A stream can mix the two kinds of item.
Bolt frames a `dict`, a `list` or a `msgspec.Struct` as one `data:` line.
A `str` or `bytes` item goes out as it is, with no SSE framing.

## Return an EventSourceResponse

Return an `EventSourceResponse` to set the status code, the headers or the ping interval.
Give it a generator object, not the generator function:

```python
@api.post("/articles/{article_id}/publish")
async def publish_article(article_id: int) -> EventSourceResponse:
    async def steps():
        yield {"step": "check"}
        await asyncio.sleep(2)
        yield {"step": "publish"}

    return EventSourceResponse(steps(), status_code=202, ping_interval=1.0)
```

```bash
curl -N -X POST localhost:8000/articles/7/publish
```

```text
data: {"step":"check"}

: ping

data: {"step":"publish"}

```

An SSE stream works with each HTTP method.

<!-- fragment -->
```python
EventSourceResponse(
    content,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    ping_interval: float | None = 15.0,
)
```

`EventSourceResponse(steps, ...)` with the function `steps` raises `TypeError`.

## Keep the connection open

A proxy or a load balancer can close a connection that sends no data.
When the generator sends no event for `ping_interval` seconds, Bolt sends the comment `: ping`.
`EventSource` ignores comments.

The default interval is 15 seconds.
A handler with `response_class=EventSourceResponse` always uses the default.
To change the interval, return an `EventSourceResponse` with `ping_interval`.
`ping_interval=None` or `0` turns off the pings.

## Clean up when the client leaves

Put the cleanup code in a `finally` block:

```python
@api.get("/articles/{article_id}/views", response_class=EventSourceResponse)
async def view_counter(article_id: int) -> AsyncIterator[dict]:
    sent = 0
    try:
        while True:
            sent += 1
            yield {"article_id": article_id, "views": sent * 10}
            await asyncio.sleep(1)
    finally:
        print(f"Reader left after {sent} events")
```

A client that stays for 2.5 seconds gets 3 events.
The server then prints `Reader left after 4 events`.

Bolt finds the disconnect when the generator yields the next item.
It then closes the generator, and the `finally` block runs.
A generator that waits a long time between items keeps its resources for that time.

## Use a sync generator

A `def` generator also works:

```python
import time
from collections.abc import Iterator


@api.get("/import/progress", response_class=EventSourceResponse)
def import_progress() -> Iterator[dict]:
    for done in (25, 50, 75, 100):
        time.sleep(0.1)
        yield {"percent": done}
```

```text
data: {"percent":25}

data: {"percent":50}

data: {"percent":75}

data: {"percent":100}

```

Each sync stream runs on its own thread for the time of the response.
Thus a blocking call in the generator does not stop other requests.
At the end of the stream, Bolt closes the database connections of that thread.

The `BOLT_MAX_SYNC_STREAMING_THREADS` setting limits the sync streams of one process.
The default is 1000.
The `DJANGO_BOLT_MAX_SYNC_STREAMING_THREADS` environment variable overrides the setting.
At the limit, a new sync stream gets only `retry: 30000` and ends.
Use an `async def` generator for many long streams.

## Stream other content

A generator handler without `response_class` gives a plain stream.
The return annotation sets the format.

### Stream JSON lines

Annotate the handler with a stream of a JSON type, for example `AsyncIterator[Article]`:

```python
import msgspec


class Article(msgspec.Struct):
    id: int
    title: str


@api.get("/articles/export")
async def export_articles() -> AsyncIterator[Article]:
    for article_id in range(1, 4):
        yield Article(id=article_id, title=f"Article {article_id}")
```

```text
{"id":1,"title":"Article 1"}
{"id":2,"title":"Article 2"}
{"id":3,"title":"Article 3"}
```

The content type is `application/x-ndjson`.
Bolt validates each item against the item type, as for a normal response.

### Stream raw text or bytes

A generator with no annotation, or with `Iterator[str]` or `Iterator[bytes]`, sends each item as it is.
The content type is `application/octet-stream`.
To set the content type or a header, return a `StreamingResponse`:

```python
from django_bolt import StreamingResponse


@api.get("/articles/export.csv")
async def export_csv() -> StreamingResponse:
    async def rows():
        yield "id,title\n"
        for article_id in range(1, 4):
            yield f"{article_id},Article {article_id}\n"

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="articles.csv"'},
    )
```

```bash
curl -i localhost:8000/articles/export.csv
```

```text
HTTP/1.1 200 OK
transfer-encoding: chunked
content-disposition: attachment; filename="articles.csv"
vary: Accept-Encoding
content-type: text/csv

id,title
1,Article 1
2,Article 2
3,Article 3
```

`StreamingResponse` takes `content`, and the keyword arguments `status_code` (default `200`), `media_type` (default `application/octet-stream`) and `headers`.
In a plain stream, a sync generator sends its items in groups of 5.
An SSE stream sends each event at once.

## Compress a stream

Bolt compresses a stream when the client accepts the encoding and compression is on.
It flushes each chunk, so each event still arrives at once.
`curl` sends no `Accept-Encoding` header, so the examples on this page show plain text.

To turn off compression for one route, use `@no_compress`:

```python
from django_bolt.middleware import no_compress


@api.get("/articles/{article_id}/plain-live", response_class=EventSourceResponse)
@no_compress
async def plain_live(article_id: int) -> AsyncIterator[dict]:
    yield {"article_id": article_id}
```

A `Content-Encoding` header that you set on a `StreamingResponse` also turns off Bolt compression for that response.
See [Compression](compression.md).

## Errors in a stream

Bolt sends the status and the headers before the first item.
Thus an exception in the generator cannot change the status code.
Bolt ends the body and logs the exception on the `django_bolt.streaming` logger.
The message is `The body of a streaming response raised an error. The response body ends here.`

A typed item that fails validation also ends the body in this way.
To report an error to an SSE client, yield an event for it, for example `ServerSentEvent(event="error", data=...)`.

## Test a stream

`TestClient` reads the full body before it returns the response.
Thus the stream must end. Give an endless generator a limit for the test, for example a query parameter.

```python title="blog/tests.py"
from django_bolt.testing import TestClient

from blog.api import api


def test_article_live_sends_three_events():
    with TestClient(api) as client:
        response = client.get("/articles/7/live")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    events = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert events == [
        'data: {"article_id":7,"readers":1}',
        'data: {"article_id":7,"readers":2}',
        'data: {"article_id":7,"readers":3}',
    ]
```

`client.get(url, stream=True)` adds `iter_lines()` and `iter_content()` to the response.
They read the body that the client already received.

## See also

- [Responses](responses.md)
- [Response classes reference](../ref/responses.md)
- [WebSocket](websocket.md)
- [Compression](compression.md)
- [Testing](testing.md)

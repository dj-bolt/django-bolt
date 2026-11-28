---
description: Compress responses with brotli, gzip or zstd. Tune CompressionConfig, turn compression off for a route, and know the BREACH risk.
---

# Compression

Django-Bolt compresses response bodies in Rust. Compression makes large JSON bodies smaller on the network.
It is on by default: brotli, with gzip for clients that do not accept brotli.

## Use the default

`BoltAPI()` compresses each response that has a body of 500 bytes or more, when the client accepts the encoding:

```python title="blog/api.py"
from django_bolt import BoltAPI

api = BoltAPI()

ARTICLES = [{"id": i, "title": f"Article {i}", "body": "Lorem ipsum " * 10} for i in range(50)]


@api.get("/articles")
async def list_articles() -> list:
    return ARTICLES
```

```bash
curl -i localhost:8000/articles -H "Accept-Encoding: br, gzip" --compressed
```

```http
HTTP/1.1 200 OK
content-type: application/json
content-encoding: br
vary: accept-encoding
```

The client chooses with its `Accept-Encoding` header. Bolt selects the encoding in this order:

1. The `backend` of the config, when the client accepts it.
2. `gzip`, when the client accepts it and `gzip_fallback` is `True`.
3. No encoding.

A `q` value of 0 rejects an encoding, for example `br;q=0`. Other `q` values do not change the order above.

Each response that Bolt can compress has `Vary: accept-encoding`, also when the client accepts no encoding and gets the plain body.
A `304` also has it when compression is on, because the `200` has it (RFC 9110, section 15.4.5).
When a compressed response has an `ETag`, the `ETag` is weak: Bolt changes `"v1"` into `W/"v1"`, as Django's `GZipMiddleware` does.
This also applies to a `StreamingResponse` that Bolt compresses.
Bolt adds no `ETag` to a response that has none.
The compressed body is another representation, and a strong `ETag` names only one (RFC 9110, section 8.8.1).
A weak `ETag` still gives `304` for `If-None-Match`, but it never matches `If-Range`.
A `304` has the weak `ETag` when the request accepts an encoding that Bolt uses and its `If-None-Match` has the weak `ETag`.
Otherwise, the `304` keeps the strong `ETag`.
A `HEAD` answer has the `Content-Encoding`, `Vary` and `ETag` of the `GET` answer, with no body (RFC 9110, section 9.3.2).
`*` accepts each encoding that the header does not name.
Bolt does not use `deflate`, and it uses `zstd` only when `zstd` is the `backend`.

| `Accept-Encoding` with the default config | `content-encoding` |
| --- | --- |
| `br, gzip` | `br` |
| `gzip` | `gzip` |
| `zstd` | none |
| no header | none |

## Configure compression

Pass a `CompressionConfig` to the API:

```python
from django_bolt import CompressionConfig

api = BoltAPI(compression=CompressionConfig(backend="gzip", minimum_size=1000))
```

| Field | Default | Effect |
| --- | --- | --- |
| `backend` | `"brotli"` | The first choice: `"brotli"`, `"gzip"` or `"zstd"` |
| `minimum_size` | `500` | Bodies smaller than this number of bytes are not compressed. Has no effect on a stream. |
| `gzip_fallback` | `True` | Use gzip when the client does not accept `backend` |
| `brotli_level` | `5` | Brotli quality, 0 to 11. Streams only. |
| `brotli_lgwin` | `14` | Brotli window, 2 to the power of this value in bytes, 10 to 24. Streams only. |
| `gzip_level` | `6` | Gzip level, 0 to 9. Streams only. |
| `zstd_level` | `3` | Zstd level, 1 to 22. Streams only. |

A value out of range raises `ValueError` when you make the config.

The level fields apply only to streaming responses, such as `StreamingResponse` and `EventSourceResponse`.
A buffered response always uses the fixed levels of the HTTP server library: brotli 3, gzip 1 and zstd 3.

`compression=False` turns compression off for the API. `compression=None` is the same as no argument: the default config.
Another value, such as `True` or a dict, raises `TypeError`.

### Use one config for the server

One server has one compression config. Bolt selects it at startup:

1. `BOLT_COMPRESSION` in `settings.py`, when you set it. `None` or `False` turns compression off.
2. Otherwise the config of the first API that has compression on.

```python title="settings.py"
from django_bolt import CompressionConfig

BOLT_COMPRESSION = CompressionConfig(backend="zstd", zstd_level=6)
```

When you serve more than one `BoltAPI`, set `BOLT_COMPRESSION`. Then the choice does not depend on the order of the APIs.
A value that is not a `CompressionConfig`, `None` or `False` raises `ImproperlyConfigured`.

## Turn compression off for a route

`@no_compress` sends the body of one route with no encoding. Put it below the route decorator:

```python
from django_bolt import no_compress


@api.get("/articles/export")
@no_compress
async def export_articles() -> list:
    return ARTICLES
```

`@no_compress` is the same as `@skip_middleware("compression")`. It works for buffered and streaming responses.

## Compress a stream

Bolt compresses `StreamingResponse` and `EventSourceResponse` too. It flushes the encoder after each chunk, so each event reaches the client at once:

```python
from django_bolt.responses import EventSourceResponse


@api.get("/articles/live")
async def live_articles() -> EventSourceResponse:
    async def events():
        for article in ARTICLES[:3]:
            yield {"id": article["id"], "title": article["title"]}

    return EventSourceResponse(events())
```

A stream keeps one encoder for each connection. A brotli encoder holds a window of 2 to the power of `brotli_lgwin` bytes: 16 KiB with the default of 14.
With many open streams, the window size sets most of the memory. Lower `brotli_lgwin` for a server with many connections.

## Send a body that you compressed

A response with a `Content-Encoding` header passes through with no change. Use it for a body that you compressed and stored:

```python
import gzip

from django_bolt import Request
from django_bolt.responses import Response

EXPORT = b'[{"id": 1, "title": "Hello"}]'
CACHED_EXPORT = gzip.compress(EXPORT)


def coding_weights(accept_encoding: str) -> dict[str, float]:
    weights = {}
    for item in accept_encoding.split(","):
        coding, *params = item.split(";")
        coding = coding.strip().lower()
        if not coding:
            continue
        weight = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        weights[coding] = weight
    return weights


def choose_coding(accept_encoding: str) -> str | None:
    weights = coding_weights(accept_encoding)
    wildcard = weights.get("*")
    if weights.get("gzip", wildcard or 0.0) > 0:
        return "gzip"
    if weights.get("identity", 1.0 if wildcard is None else wildcard) > 0:
        return "identity"
    return None


@api.get("/articles/cached")
async def cached_export(request: Request) -> Response:
    vary = {"Vary": "Accept-Encoding"}
    coding = choose_coding(request.headers.get("accept-encoding", ""))
    if coding == "gzip":
        return Response(CACHED_EXPORT, media_type="application/json", headers={**vary, "Content-Encoding": "gzip"})
    if coding == "identity":
        return Response(EXPORT, media_type="application/json", headers=vary)
    return Response({"detail": "No acceptable encoding"}, status_code=406, headers=vary)
```

Bolt does not check that the body matches the header, and it does not check the `Accept-Encoding` of the client. Thus the handler checks it.
`gzip;q=0` refuses gzip, and `*` accepts each encoding that the header does not name. A `q` value that is not a number refuses its encoding.
A client that refuses gzip and the plain body gets `406`.
Each answer has `Vary: Accept-Encoding`. Without it, a shared cache can send the gzip body to a client that cannot read it.

## Avoid the BREACH attack

Compression can leak a secret. An attacker who can put text into a compressed response, and who can see its size, can guess a secret in the same response one byte at a time.
This is the [BREACH](https://en.wikipedia.org/wiki/BREACH) attack. An event stream shows the size of each event, which makes the attack easier.

When a response holds a secret, such as a token or a CSRF value, and also text that a user controls:

- Turn compression off for the route with `@no_compress`.
- Or send the secret in another response or a header.

## See also

- [Middleware reference](../ref/middleware.md#compressionconfig): `CompressionConfig` and `@no_compress`.
- [Server-sent events](sse.md): `EventSourceResponse`.
- [Responses](responses.md): `StreamingResponse` and custom headers.
- [Settings reference](../ref/settings.md): `BOLT_COMPRESSION`.

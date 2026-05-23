# Compression

Django-Bolt compresses both buffered and streaming responses through a
single configuration. The same `CompressionConfig` controls every codec
choice, the same `@no_compress` decorator opts a route out, and the same
client-side `Accept-Encoding` negotiation picks the encoding. Streaming
responses (SSE, async generators) just add a per-chunk flush so events
reach the client one at a time instead of waiting for the encoder's
internal block to fill.

## Quick start

Compression is on by default. Use the framework's defaults and you get
brotli with gzip fallback for every eligible response:

```python
from django_bolt import BoltAPI
from django_bolt.responses import EventSourceResponse
from django_bolt.middleware import CompressionConfig, no_compress

api = BoltAPI()  # brotli, gzip fallback, on for buffered + streaming


# Buffered JSON — compressed when body ≥ minimum_size and client accepts br/gzip
@api.get("/users")
async def users():
    return [{"id": i, "name": f"user-{i}"} for i in range(1000)]


# Streaming SSE — each event compressed and flushed individually
@api.get("/sse/feed")
async def feed():
    async def gen():
        for i in range(20):
            yield {"i": i, "msg": "x" * 200}

    return EventSourceResponse(gen())


# Per-route opt-out — works the same for buffered and streaming
@api.get("/raw")
@no_compress
async def raw():
    return {"plain": True}
```

Disable compression entirely by passing `compression=False`:

```python
api = BoltAPI(compression=False)
```

(`compression=None` and omitting the kwarg both mean "use the default
`CompressionConfig()`".)

`StreamingResponse` / `EventSourceResponse` don't take a `compress=`
kwarg — the decision is always made per-request from the global
`CompressionConfig` plus the client's `Accept-Encoding`.

## CompressionConfig

```python
from django_bolt import BoltAPI
from django_bolt.middleware import CompressionConfig

api = BoltAPI(compression=CompressionConfig(
    backend="brotli",        # "brotli" | "gzip" | "zstd"
    minimum_size=500,        # buffered-only; ignored for streams
    gzip_fallback=True,      # use gzip if client doesn't accept backend
    brotli_level=5,          # 0..=11
    brotli_lgwin=14,         # 10..=24 — 2^lgwin bytes window per stream
    gzip_level=6,            # 0..=9
    zstd_level=3,            # 1..=22
))
```

| Field | Buffered | Streaming | Notes |
|---|---|---|---|
| `backend` | yes | yes | Preferred codec when client accepts it. |
| `minimum_size` | yes | no effect | Streams don't have a known total size. |
| `gzip_fallback` | yes | yes | Wrap with gzip when client doesn't accept `backend`. |
| `brotli_level` / `gzip_level` / `zstd_level` | yes | yes | CPU/ratio tradeoff. |
| `brotli_lgwin` | yes | yes | Per-stream memory knob — important for high-fanout SSE. |

## Negotiation

For every response (buffered or streaming) the same algorithm runs:

1. `@no_compress` on the route → no compression.
2. `BoltAPI(compression=False)` → no compression.
3. Client accepts `cfg.backend` → wrap with that codec.
4. Else `gzip_fallback=True` and client accepts `gzip` → wrap with gzip.
5. Else → no compression.

When no compression is applied, `Content-Encoding` is absent on the
wire (the framework uses `identity` as an internal "skip" marker that
the middleware strips before sending).

`Accept-Encoding: *` accepts any unmentioned coding; `*;q=0` rejects
them. An explicit `br;q=0` rejects brotli even when `*` is generous.

## Streaming behavior

Streaming responses go through a per-chunk *sync flush* so the wire
sees a self-contained compressed block after every `yield`. Without
this flush the encoder would buffer events into its internal block and
the client wouldn't see anything until the block filled — fatal for
SSE and async generators.

| Codec  | Flush mechanism                             | Decodable per chunk |
|--------|---------------------------------------------|---------------------|
| brotli | `CompressorWriter::flush()` → `BROTLI_OPERATION_FLUSH` | yes |
| gzip   | `GzEncoder::flush()` → `Z_SYNC_FLUSH`        | yes                 |
| zstd   | `zstd::stream::write::Encoder::flush()`     | yes                 |

One encoder runs per connection, so cross-chunk dictionary reuse still
helps the ratio. Per-event flushing caps the achievable ratio compared
to one-shot compression (each flush ends a self-contained block), so
high quality levels buy less on streams than on buffered responses.

## Per-connection memory (brotli `lgwin`)

`brotli_lgwin` is the dominant memory knob for streaming compression.
Window size = `2^lgwin` bytes **per active connection**. The default of
`14` gives a 16 KiB window — enough for SSE event vocabulary reuse,
cheap enough for high-fanout servers.

| `lgwin`              | Window  |
|----------------------|---------|
| 10 (min)             | 1 KiB   |
| 14 (**default**)     | 16 KiB  |
| 16                   | 64 KiB  |
| 18                   | 256 KiB |
| 20                   | 1 MiB   |
| 22 (brotli "normal") | 4 MiB   |
| 24 (max)             | 16 MiB  |

Memory cost scales with `2^lgwin` per active connection (plus encoder
overhead), so the window choice dominates resident memory under high
fanout. Drop `lgwin` for high-fanout SSE; raise it for large,
repetitive buffered bodies where the per-request cost is amortized
over a single response.

## Compression levels

Level fields trade CPU for ratio. Defaults are tuned for the streaming
case (per-chunk flush caps achievable ratio, so spending CPU on level
11 buys little).

- **`brotli_level`** (0..=11) — `5` is balanced. The highest levels
  trade significant CPU for a small ratio win and are typically only
  worth it for static/precompressed assets.
- **`gzip_level`** (0..=9) — `6` matches zlib's `Z_DEFAULT_COMPRESSION`.
- **`zstd_level`** (1..=22) — `3` is balanced. Levels above 19 enable
  "ultra" mode and are very memory- and CPU-heavy.

## Interaction with the global compression middleware

The buffered compression middleware reads `Content-Encoding` on the
outgoing response: if any value is pre-set, it passes the body through
unchanged. Streaming compression runs inside the handler and pre-sets
`Content-Encoding`, so the global middleware never re-wraps a stream —
no double-compression possible.

## Security — CRIME / BREACH

Compressing responses that mix attacker-influenced content with secrets
(session tokens, cookies, CSRF values, user identifiers) is vulnerable
to compression-ratio side-channel attacks. SSE is a particularly easy
target: per-event sizes are directly observable, and an attacker who
controls part of an event's content can probe for secret bytes one at
a time.

If your payloads can contain both attacker-controllable data **and**
secrets, either:

- Don't compress those responses (`@no_compress`), or
- Move the secret elsewhere (header, separate channel) so the
  compressed body never sees it.

This is the same risk class as HTTPS-level compression
([CRIME](https://en.wikipedia.org/wiki/CRIME) /
[BREACH](https://en.wikipedia.org/wiki/BREACH)); the framework
intentionally leaves the decision in your hands per-route rather than
disabling compression globally.

## Implementation pointers

- `src/middleware/compression.rs` — buffered compression middleware;
  bypasses any pre-set `Content-Encoding` so handler-owned encodings
  (streaming or otherwise) aren't re-wrapped.
- `src/streaming_compression.rs` — `StreamCodec` enum, `EncoderStream`
  generic adapter, `select_stream_encoding`, zero-alloc Accept-Encoding
  parser.
- `src/streaming.rs::maybe_wrap_codec` — boxes the encoder around the
  Python chunk stream (and the keep-alive wrapper for SSE).
- `src/handler.rs::build_response_from_parsed` — reads
  `AppState.global_compression_config`, runs negotiation, sets the
  encoding headers, and wraps the stream.
- `python/django_bolt/middleware/compression.py` — `CompressionConfig`
  dataclass and per-codec level/range types.

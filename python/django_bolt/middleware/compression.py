"""
Compression configuration for Django-Bolt.

Provides configuration options for response compression (gzip, brotli, zstd).
Compression levels are handled automatically by Actix Web with optimized defaults.
"""

from dataclasses import dataclass
from typing import Literal


# Static type aliases for the per-codec tuning fields. The `Literal` types
# are checker-only: mypy/pyright will flag obvious out-of-range literals at
# author time, but Python does not enforce them at runtime. `_check_int_range`
# enforces the same ranges in `__post_init__` for values that arrive
# dynamically (e.g. config built from env vars or untyped dicts).
BrotliLevel = Literal[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
"""Brotli quality/compression level (0..=11).

Higher = smaller output but more CPU per response. The high end is *very*
expensive — 11 can be 10-100× slower than 5 for a marginal size win.

- 0  — fastest, almost no compression
- 4  — fast, decent ratio (good for streaming / low-latency)
- 5  — balanced default
- 6  — close to gzip-6 cost, better ratio
- 11 — best ratio, only sensible for static/precompressed assets
"""

BrotliLgWin = Literal[10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
"""Brotli sliding window log size (10..=24). Window size in bytes = 2^lgwin.

Larger windows compress repetitive payloads better but cost ~2^lgwin bytes
of memory **per active stream**. For high-fanout SSE / WebSocket servers
this is the dominant per-connection memory knob, not the quality level.

- 10 — 1 KiB    (lowest memory, weakest ratio)
- 14 — 16 KiB   (default; good for streams and small JSON bodies)
- 18 — 256 KiB  (old default; better ratio for large bodies)
- 22 — 4 MiB    (best ratio for big static assets)
- 24 — 16 MiB   (max; rarely worth it outside batch/offline use)
"""

GzipLevel = Literal[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
"""Gzip/deflate compression level (0..=9).

Higher = smaller output but more CPU per response. Returns diminish sharply
past 6; 9 is rarely worth the extra cost for dynamic responses.

- 0 — store only (no compression)
- 1 — fastest
- 6 — balanced default (matches zlib's `Z_DEFAULT_COMPRESSION`)
- 9 — best ratio, slowest
"""

ZstdLevel = Literal[
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
    12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
]
"""Zstd compression level (1..=22).

Higher = smaller output but more CPU per response. Levels above ~19 enable
"ultra" mode and are very memory- and CPU-heavy — usually only worth it for
precompressed static assets.

- 1  — fastest
- 3  — balanced default
- 9  — strong ratio, still reasonable for dynamic responses
- 19 — top of the "normal" range
- 22 — max ratio, very slow and memory-hungry
"""


def _check_int_range(name: str, value: object, lo: int, hi: int) -> None:
    # `bool` is an `int` subclass — reject it explicitly so True/False can't
    # sneak through as 0/1 (a footgun for tuning fields).
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        raise ValueError(f"{name} must be an int in [{lo}, {hi}], got {value!r}")


@dataclass
class CompressionConfig:
    """Configuration for response compression.

    Applies to both buffered and streaming responses. Streaming responses
    (``StreamingResponse``, ``EventSourceResponse``) use a per-chunk flush
    encoder so events still arrive at the client one at a time. Buffered
    responses go through the global compression middleware.

    Per-route opt-out: ``@no_compress``.

    Args:
        backend: Compression backend to use (default: "brotli").
            One of "gzip", "brotli", "zstd".
        minimum_size: Minimum buffered-response size in bytes to compress
            (default: 500). Has no effect on streaming responses.
        gzip_fallback: Use gzip if the client doesn't accept the configured
            backend (default: True). Applies to streaming responses too.
        brotli_level: Brotli quality/level 0..=11 (default: 5).
        brotli_lgwin: Brotli sliding window log size 10..=24 (default: 14 →
            16 KiB window). Dominant per-connection memory knob for streams.
        gzip_level: Gzip compression level 0..=9 (default: 6).
        zstd_level: Zstd compression level 1..=22 (default: 3).

    Examples:
        # Defaults — brotli, gzip fallback, on for every response (buffered + streaming)
        api = BoltAPI(compression=CompressionConfig())

        # Smaller window for high-fanout SSE servers
        api = BoltAPI(compression=CompressionConfig(brotli_lgwin=12))

        # Gzip backend, no fallback
        api = BoltAPI(compression=CompressionConfig(
            backend="gzip",
            gzip_fallback=False,
            gzip_level=6,
        ))

        # Disable compression entirely on this BoltAPI
        api = BoltAPI(compression=False)
    """

    backend: Literal["gzip", "brotli", "zstd"] = "brotli"
    minimum_size: int = 500
    gzip_fallback: bool = True
    brotli_level: BrotliLevel = 5
    brotli_lgwin: BrotliLgWin = 14
    gzip_level: GzipLevel = 6
    zstd_level: ZstdLevel = 3

    def __post_init__(self):
        valid_backends = {"gzip", "brotli", "zstd"}
        if self.backend not in valid_backends:
            raise ValueError(f"Invalid backend: {self.backend}. Must be one of {valid_backends}")
        if self.minimum_size < 0:
            raise ValueError("minimum_size must be non-negative")
        _check_int_range("brotli_level", self.brotli_level, 0, 11)
        _check_int_range("brotli_lgwin", self.brotli_lgwin, 10, 24)
        _check_int_range("gzip_level", self.gzip_level, 0, 9)
        _check_int_range("zstd_level", self.zstd_level, 1, 22)

    def to_rust_config(self) -> dict:
        return {
            "backend": self.backend,
            "minimum_size": self.minimum_size,
            "gzip_fallback": self.gzip_fallback,
            "brotli_level": self.brotli_level,
            "brotli_lgwin": self.brotli_lgwin,
            "gzip_level": self.gzip_level,
            "zstd_level": self.zstd_level,
        }

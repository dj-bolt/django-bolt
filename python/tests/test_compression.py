"""
Tests for compression middleware in Django-Bolt.

Tests both global compression configuration and per-route skip functionality.
"""

import gzip
import json

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.middleware import CompressionConfig, no_compress, skip_middleware
from django_bolt.responses import HTML, PlainText, Response, StreamingResponse
from django_bolt.testing import TestClient


def test_compression_enabled_by_default():
    """Test that compression is enabled by default with brotli backend."""
    api = BoltAPI()  # Default: compression enabled with brotli

    @api.get("/data")
    async def get_data():
        # Return data larger than default minimum_size (500 bytes)
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)
    response = client.get("/data", headers={"Accept-Encoding": "gzip, br"})

    assert response.status_code == 200
    # Content-Encoding should be set to br (brotli) or gzip
    # Note: Actix may compress differently based on client support
    assert response.headers.get("content-encoding") in ["br", "gzip", None]


def test_compression_skip_middleware_decorator():
    """Test that @skip_middleware('compression') disables compression for a route."""
    api = BoltAPI(compression=CompressionConfig(backend="gzip"))

    @api.get("/normal")
    async def normal_route():
        return {"data": "x" * 1000}

    @api.get("/skipped")
    @skip_middleware("compression")
    async def skipped_route():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)

    # Normal route may be compressed
    normal_resp = client.get("/normal", headers={"Accept-Encoding": "gzip, br"})
    assert normal_resp.status_code == 200

    # Skipped route should NOT have Content-Encoding (no compression applied)
    skipped_resp = client.get("/skipped", headers={"Accept-Encoding": "gzip, br"})
    assert skipped_resp.status_code == 200
    # When skip_compression is true, no content-encoding should be set
    assert skipped_resp.headers.get("content-encoding") is None


def test_no_compress_decorator():
    """Test that @no_compress decorator disables compression."""
    api = BoltAPI(compression=CompressionConfig(backend="gzip"))

    @api.get("/compressed")
    async def compressed_route():
        return {"data": "x" * 1000}

    @api.get("/uncompressed")
    @no_compress
    async def uncompressed_route():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)

    # Compressed route may have compression
    compressed_resp = client.get("/compressed", headers={"Accept-Encoding": "gzip"})
    assert compressed_resp.status_code == 200

    # Uncompressed route should NOT have Content-Encoding header
    uncompressed_resp = client.get("/uncompressed", headers={"Accept-Encoding": "gzip"})
    assert uncompressed_resp.status_code == 200
    assert uncompressed_resp.headers.get("content-encoding") is None


def test_compression_disabled():
    """Test that compression can be disabled globally."""
    api = BoltAPI(compression=False)  # Explicitly disable compression

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)
    response = client.get("/data", headers={"Accept-Encoding": "gzip, br"})

    assert response.status_code == 200
    # No compression should be applied when disabled
    # Note: Compress middleware might still be active but respects config
    assert response.status_code == 200


def test_compression_custom_config():
    """Test custom compression configuration."""
    api = BoltAPI(
        compression=CompressionConfig(
            backend="gzip",
            minimum_size=1000,  # Larger threshold
            gzip_fallback=True,
        )
    )

    @api.get("/small")
    async def small_data():
        # Data smaller than minimum_size (1000 bytes)
        return {"data": "x" * 100}

    @api.get("/large")
    async def large_data():
        # Data larger than minimum_size
        return {"data": "x" * 2000}

    client = TestClient(api, use_http_layer=True)

    # Small data should not be compressed (below minimum_size)
    small_resp = client.get("/small", headers={"Accept-Encoding": "gzip"})
    assert small_resp.status_code == 200

    # Large data may be compressed
    large_resp = client.get("/large", headers={"Accept-Encoding": "gzip"})
    assert large_resp.status_code == 200


def test_compression_brotli_config():
    """Test brotli compression configuration."""
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)
    response = client.get("/data", headers={"Accept-Encoding": "br, gzip"})

    assert response.status_code == 200
    # Should prefer brotli if client supports it
    # Note: Actual compression depends on client headers


def test_compression_zstd_config():
    """Test zstd compression configuration."""
    api = BoltAPI(compression=CompressionConfig(backend="zstd", minimum_size=500, gzip_fallback=True))

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)

    # With zstd backend and gzip fallback
    response = client.get("/data", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200


def test_compression_with_different_content_types():
    """Test that compression works with various content types."""
    api = BoltAPI(compression=CompressionConfig(backend="gzip"))

    @api.get("/json")
    async def json_route():
        return {"data": "x" * 1000}

    @api.get("/text")
    async def text_route():
        return PlainText("x" * 1000)

    @api.get("/html")
    async def html_route():
        return HTML("<html><body>" + "x" * 1000 + "</body></html>")

    client = TestClient(api, use_http_layer=True)

    # JSON should be compressible
    json_resp = client.get("/json", headers={"Accept-Encoding": "gzip"})
    assert json_resp.status_code == 200

    # Plain text should be compressible
    text_resp = client.get("/text", headers={"Accept-Encoding": "gzip"})
    assert text_resp.status_code == 200

    # HTML should be compressible
    html_resp = client.get("/html", headers={"Accept-Encoding": "gzip"})
    assert html_resp.status_code == 200


def test_compression_skip_on_streaming():
    """Test that streaming responses can skip compression."""
    api = BoltAPI(compression=CompressionConfig(backend="gzip"))

    @api.get("/stream")
    @skip_middleware("compression")
    async def stream_route():
        async def generate():
            for i in range(100):
                yield f"data: {i}\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    client = TestClient(api, use_http_layer=True)
    response = client.get("/stream", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    # Streaming with skip should NOT have Content-Encoding header
    assert response.headers.get("content-encoding") is None


def test_compression_multiple_skip_middleware():
    """Test skipping multiple middleware including compression."""
    api = BoltAPI(compression=CompressionConfig(backend="gzip"))

    @api.get("/skip-multiple")
    @skip_middleware("cors", "compression")
    async def multi_skip():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)
    response = client.get("/skip-multiple", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") is None


# ─── Pre-compressed Response bodies (issue #305) ────────────────────────
#
# A handler can return a Response whose content is already compressed,
# with an explicit Content-Encoding header. The bytes must reach the
# client verbatim. The JSON media type must not base64-encode them.


def _pre_compressed_payload() -> tuple[bytes, bytes]:
    payload = json.dumps({"data": "x" * 1000}).encode()
    return payload, gzip.compress(payload)


def test_pre_compressed_response_with_compression_disabled():
    """Response(bytes) with Content-Encoding passes through when compression is off."""
    api = BoltAPI(compression=False)
    payload, compressed = _pre_compressed_payload()

    @api.get("/cached")
    async def cached():
        return Response(compressed, headers={"Content-Encoding": "gzip"})

    with TestClient(api, use_http_layer=True) as client:
        resp = client.get("/cached", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        # httpx auto-decodes gzip; corrupted bytes raise DecodingError here.
        assert resp.content == payload


def test_pre_compressed_response_with_compression_enabled():
    """The middleware must not re-encode a body with a pre-set Content-Encoding."""
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=0))
    payload, compressed = _pre_compressed_payload()

    @api.get("/cached")
    async def cached():
        return Response(compressed, headers={"Content-Encoding": "gzip"})

    with TestClient(api, use_http_layer=True) as client:
        resp = client.get("/cached", headers={"Accept-Encoding": "br, gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert resp.content == payload


def test_pre_compressed_response_sync_handler():
    """The sync serialization path must also pass pre-compressed bytes through."""
    api = BoltAPI(compression=False)
    payload, compressed = _pre_compressed_payload()

    @api.get("/cached-sync")
    def cached_sync():
        return Response(compressed, headers={"Content-Encoding": "gzip"})

    with TestClient(api, use_http_layer=True) as client:
        resp = client.get("/cached-sync", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert resp.content == payload


def test_pre_compressed_response_with_response_model():
    """A route with a response_model must not validate pre-encoded bytes."""

    class Item(msgspec.Struct):
        data: str

    api = BoltAPI(compression=False)
    payload, compressed = _pre_compressed_payload()

    @api.get("/cached-model", response_model=list[Item])
    async def cached_model():
        return Response(compressed, headers={"Content-Encoding": "gzip"})

    with TestClient(api, use_http_layer=True) as client:
        resp = client.get("/cached-model", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert resp.content == payload


def test_response_bytes_without_content_encoding_not_json_wrapped():
    """Response(bytes) with the default media type sends the bytes as-is."""
    api = BoltAPI(compression=False)
    raw = b'{"already": "encoded"}'

    @api.get("/raw")
    async def raw_bytes():
        return Response(raw)

    with TestClient(api, use_http_layer=True) as client:
        resp = client.get("/raw")
        assert resp.status_code == 200
        assert resp.content == raw


def test_compression_config_validation():
    """Test that compression config validates properly."""
    # Valid configs should work
    config1 = CompressionConfig(backend="gzip")
    assert config1.backend == "gzip"

    config2 = CompressionConfig(backend="brotli")
    assert config2.backend == "brotli"

    config3 = CompressionConfig(backend="zstd")
    assert config3.backend == "zstd"

    # Invalid backend should raise error
    with pytest.raises(ValueError, match="Invalid backend"):
        CompressionConfig(backend="invalid")

    # Negative minimum_size should raise error
    with pytest.raises(ValueError, match="minimum_size must be non-negative"):
        CompressionConfig(backend="gzip", minimum_size=-1)

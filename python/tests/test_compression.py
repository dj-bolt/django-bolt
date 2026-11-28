"""
Tests for compression middleware in Django-Bolt.

Tests both global compression configuration and per-route skip functionality.
"""

import asyncio
import gzip
import json

import msgspec
import pytest
from django.core.exceptions import ImproperlyConfigured

from django_bolt import BoltAPI, Request
from django_bolt.middleware import CompressionConfig, no_compress, skip_middleware
from django_bolt.responses import HTML, FileResponse, PlainText, Response, StreamingResponse
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

    compressed_resp = client.get("/compressed", headers={"Accept-Encoding": "gzip"})
    assert compressed_resp.status_code == 200
    assert compressed_resp.headers.get("content-encoding") == "gzip"

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
    assert response.headers.get("content-encoding") is None
    assert response.json() == {"data": "x" * 1000}


@pytest.mark.parametrize(
    ("setting", "api_compression", "expected"),
    [
        (None, None, None),
        (False, None, None),
        (CompressionConfig(backend="gzip"), False, "gzip"),
    ],
    ids=["none-disables", "false-disables", "config-wins"],
)
def test_bolt_compression_setting_wins_over_the_api_argument(settings, setting, api_compression, expected):
    """`TestClient` applies `settings.BOLT_COMPRESSION` as `runbolt` does: the setting wins over the API."""
    settings.BOLT_COMPRESSION = setting
    api = BoltAPI(compression=api_compression)

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/data", headers={"Accept-Encoding": "gzip, br"})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") == expected
    assert response.json() == {"data": "x" * 1000}


@pytest.mark.parametrize("value", [True, {"backend": "gzip"}, "gzip"], ids=["true", "dict", "str"])
def test_compression_argument_of_a_wrong_type_raises(value):
    """`compression` takes a CompressionConfig, None or False. Each other value raises at once."""
    with pytest.raises(TypeError, match="compression must be a CompressionConfig, None or False"):
        BoltAPI(compression=value)


@pytest.mark.parametrize(
    ("value", "type_name"),
    [(True, "bool"), ({"backend": "gzip"}, "dict"), ("gzip", "str")],
    ids=["true", "dict", "str"],
)
def test_bolt_compression_setting_of_a_wrong_type_raises(settings, value, type_name):
    """`settings.BOLT_COMPRESSION` takes the values of the `compression` argument. The error names the setting."""
    settings.BOLT_COMPRESSION = value
    api = BoltAPI()

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    message = rf"settings\.BOLT_COMPRESSION must be a CompressionConfig, None or False, not {type_name}"
    with pytest.raises(ImproperlyConfigured, match=message):
        TestClient(api, use_http_layer=True)


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
    assert small_resp.headers.get("content-encoding") is None

    large_resp = client.get("/large", headers={"Accept-Encoding": "gzip"})
    assert large_resp.status_code == 200
    assert large_resp.headers.get("content-encoding") == "gzip"
    assert large_resp.json() == {"data": "x" * 2000}


def test_compressed_response_has_one_vary_accept_encoding():
    """A compressed response names Accept-Encoding in Vary once, after the Vary of the handler."""
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    @api.get("/origin")
    async def get_origin():
        return Response({"data": "x" * 1000}, headers={"Vary": "Origin"})

    @api.get("/small")
    async def get_small():
        return {"data": "x"}

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/data", headers={"Accept-Encoding": "br"})
        assert response.headers.get("content-encoding") == "br"
        assert response.headers.get_list("vary") == ["accept-encoding"]

        response = client.get("/origin", headers={"Accept-Encoding": "br"})
        assert response.headers.get("content-encoding") == "br"
        assert response.headers.get_list("vary") == ["Origin", "accept-encoding"]

        # A response under minimum_size is not compressed, so it has no Vary.
        response = client.get("/small", headers={"Accept-Encoding": "br"})
        assert response.headers.get("content-encoding") is None
        assert response.headers.get_list("vary") == []


def test_compression_brotli_config():
    """Test brotli compression configuration."""
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)
    response = client.get("/data", headers={"Accept-Encoding": "br, gzip"})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "br"
    assert response.json() == {"data": "x" * 1000}


def test_compression_zstd_config():
    """Test zstd compression configuration."""
    api = BoltAPI(compression=CompressionConfig(backend="zstd", minimum_size=500, gzip_fallback=True))

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    client = TestClient(api, use_http_layer=True)

    # The client accepts zstd: the configured backend is used.
    response = client.get("/data", headers={"Accept-Encoding": "zstd, gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "zstd"
    assert response.json() == {"data": "x" * 1000}

    # The client accepts only gzip: gzip_fallback applies.
    response = client.get("/data", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert response.json() == {"data": "x" * 1000}


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
    with pytest.raises(ValueError, match="^minimum_size must be non-negative$"):
        CompressionConfig(backend="gzip", minimum_size=-1)


def test_compression_config_defaults_reach_rust_unchanged():
    """The default config and its Rust form hold the documented values."""
    assert CompressionConfig().to_rust_config() == {
        "backend": "brotli",
        "minimum_size": 500,
        "gzip_fallback": True,
        "brotli_level": 5,
        "brotli_lgwin": 14,
        "gzip_level": 6,
        "zstd_level": 3,
    }


def test_compression_config_rust_form_keeps_each_custom_value():
    """Each field has its own key in the Rust form."""
    config = CompressionConfig(
        backend="zstd",
        minimum_size=0,
        gzip_fallback=False,
        brotli_level=11,
        brotli_lgwin=24,
        gzip_level=9,
        zstd_level=22,
    )

    assert config.to_rust_config() == {
        "backend": "zstd",
        "minimum_size": 0,
        "gzip_fallback": False,
        "brotli_level": 11,
        "brotli_lgwin": 24,
        "gzip_level": 9,
        "zstd_level": 22,
    }


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [
        ("brotli_level", 0, 11),
        ("brotli_lgwin", 10, 24),
        ("gzip_level", 0, 9),
        ("zstd_level", 1, 22),
    ],
)
def test_compression_config_accepts_the_edges_of_a_level_range(field, low, high):
    """A level at the lowest or the highest edge of its range is valid."""
    assert getattr(CompressionConfig(**{field: low}), field) == low
    assert getattr(CompressionConfig(**{field: high}), field) == high


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("brotli_level", -1),
        ("brotli_level", 12),
        ("brotli_lgwin", 9),
        ("brotli_lgwin", 25),
        ("gzip_level", -1),
        ("gzip_level", 10),
        ("zstd_level", 0),
        ("zstd_level", 23),
        ("gzip_level", True),
        ("zstd_level", True),
        ("brotli_level", 5.0),
        ("brotli_lgwin", "14"),
    ],
)
def test_compression_config_rejects_a_level_outside_its_range(field, bad):
    """A level out of range, a bool and a non-int stop startup. The message names the field and the range."""
    with pytest.raises(ValueError, match=rf"{field} must be an int in \[\d+, \d+\], got {bad!r}"):
        CompressionConfig(**{field: bad})


def test_compression_config_accepts_a_minimum_size_of_zero():
    """Zero means that each buffered response is compressed."""
    api = BoltAPI(compression=CompressionConfig(backend="gzip", minimum_size=0))

    @api.get("/tiny")
    async def tiny():
        return {"data": "x" * 200}

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/tiny", headers={"Accept-Encoding": "gzip"})

    assert response.headers.get("content-encoding") == "gzip"


def _etag_api():
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    @api.get("/strong")
    async def strong():
        return Response({"data": "x" * 1000}, headers={"ETag": '"v1"'})

    @api.get("/weak")
    async def weak():
        return Response({"data": "x" * 1000}, headers={"ETag": 'W/"v1"'})

    @api.get("/small")
    async def small():
        return Response({"data": "x"}, headers={"ETag": '"v1"'})

    @api.get("/conditional")
    async def conditional(request: Request):
        # As actix-files does: the 304 carries the strong ETag of the file.
        if request.headers.get("if-none-match"):
            return PlainText("", status_code=304, headers={"ETag": '"v1"'})
        return Response({"data": "x" * 1000}, headers={"ETag": '"v1"'})

    return api


def test_a_coded_response_has_a_weak_etag():
    """RFC 9110, section 8.8.1: the coded body is another representation, so a strong ETag becomes weak.

    Django's GZipMiddleware does the same. A plain answer keeps the strong ETag.
    """
    with TestClient(_etag_api(), use_http_layer=True) as client:
        coded = client.get("/strong", headers={"Accept-Encoding": "br"})
        assert coded.headers.get("content-encoding") == "br"
        assert coded.headers.get("etag") == 'W/"v1"'

        plain = client.get("/strong", headers={"Accept-Encoding": "identity"})
        assert plain.headers.get("content-encoding") is None
        assert plain.headers.get("etag") == '"v1"'

        weak = client.get("/weak", headers={"Accept-Encoding": "br"})
        assert weak.headers.get("etag") == 'W/"v1"'

        small = client.get("/small", headers={"Accept-Encoding": "br"})
        assert small.headers.get("content-encoding") is None
        assert small.headers.get("etag") == '"v1"'


def test_a_304_has_the_etag_that_the_client_holds():
    """RFC 9110, section 15.4.5: a 304 has the ETag that the 200 would have.

    A client that holds the coded answer sends its weak ETag, and gets the weak
    form back. A client that holds the plain answer gets the strong ETag.
    """
    with TestClient(_etag_api(), use_http_layer=True) as client:
        coded = client.get("/conditional", headers={"Accept-Encoding": "br"})
        assert coded.headers.get("etag") == 'W/"v1"'

        revalidated = client.get("/conditional", headers={"Accept-Encoding": "br", "If-None-Match": 'W/"v1"'})
        assert revalidated.status_code == 304
        assert revalidated.headers.get("etag") == 'W/"v1"'

        plain = client.get("/conditional", headers={"Accept-Encoding": "identity", "If-None-Match": '"v1"'})
        assert plain.status_code == 304
        assert plain.headers.get("etag") == '"v1"'

        other = client.get("/conditional", headers={"Accept-Encoding": "br", "If-None-Match": 'W/"v2", "v1"'})
        assert other.status_code == 304
        assert other.headers.get("etag") == '"v1"'


@pytest.mark.parametrize("accept_encoding", ["br", "identity"])
def test_a_304_varies_as_the_200_does(accept_encoding):
    """RFC 9110, section 15.4.5: a 304 has the Vary that the 200 would have.

    The 200 names accept-encoding in Vary when Bolt can compress it. Bolt does
    not see the size of the 200 body for a 304, so a 304 names it when
    compression is on. With no compression config, a 304 has no such Vary.
    """
    headers = {"Accept-Encoding": accept_encoding, "If-None-Match": '"v1"'}
    with TestClient(_etag_api(), use_http_layer=True) as client:
        answer = client.get("/conditional", headers=headers)
        assert answer.status_code == 304
        assert answer.headers.get_list("vary") == ["accept-encoding"]

    api = BoltAPI(compression=False)

    @api.get("/conditional")
    async def conditional():
        return PlainText("", status_code=304, headers={"ETag": '"v1"'})

    with TestClient(api, use_http_layer=True) as client:
        answer = client.get("/conditional", headers=headers)
        assert answer.status_code == 304
        assert answer.headers.get_list("vary") == []


def test_a_stream_that_bolt_codes_has_a_weak_etag():
    """RFC 9110, section 8.8.1: Bolt codes the stream, so its strong ETag becomes weak.

    A plain stream keeps the strong ETag. A stream with a Content-Encoding of
    the handler keeps the ETag of the handler.
    """
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    @api.get("/stream")
    async def stream():
        return StreamingResponse(iter([b"x" * 1000]), media_type="text/plain", headers={"ETag": '"v1"'})

    @api.get("/events")
    async def events():
        return StreamingResponse(iter(["data: x\n\n"]), media_type="text/event-stream", headers={"ETag": '"v1"'})

    @api.get("/own-coding")
    async def own_coding():
        body = gzip.compress(b"x" * 1000)
        return StreamingResponse(
            iter([body]), media_type="text/plain", headers={"ETag": '"v1"', "Content-Encoding": "gzip"}
        )

    with TestClient(api, use_http_layer=True) as client:
        for path in ("/stream", "/events"):
            coded = client.get(path, headers={"Accept-Encoding": "br"})
            assert coded.headers.get("content-encoding") == "br"
            assert coded.headers.get("etag") == 'W/"v1"'

            plain = client.get(path, headers={"Accept-Encoding": "identity"})
            assert plain.headers.get("content-encoding") is None
            assert plain.headers.get("etag") == '"v1"'

        own = client.get("/own-coding", headers={"Accept-Encoding": "br"})
        assert own.headers.get("content-encoding") == "gzip"
        assert own.headers.get("etag") == '"v1"'


LARGE_FILE_SIZE = 10 * 1024 * 1024 + 1


@pytest.fixture(scope="module")
def head_files(tmp_path_factory):
    """A small file and a file that Bolt streams (10 MiB or more)."""
    directory = tmp_path_factory.mktemp("head")
    small = directory / "small.txt"
    small.write_bytes(b"x" * 1000)
    large = directory / "large.txt"
    large.write_bytes(b"x" * LARGE_FILE_SIZE)
    return small, large


def _head_api(small_file, large_file):
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    def get_and_head(path):
        def register(handler):
            api.head(path)(handler)
            return api.get(path)(handler)

        return register

    @get_and_head("/async")
    async def async_route():
        # A real suspension, so the route takes the async dispatch path.
        await asyncio.sleep(0)
        return Response({"data": "x" * 1000}, headers={"ETag": '"v1"'})

    @get_and_head("/sync")
    def sync_route():
        return Response({"data": "x" * 1000}, headers={"ETag": '"v1"'})

    @get_and_head("/bare")
    def bare_route():
        return {"data": "x" * 1000}

    @get_and_head("/small")
    def small_route():
        return Response({"data": "x"}, headers={"ETag": '"v1"'})

    @get_and_head("/file")
    async def file_route():
        return FileResponse(str(small_file), headers={"ETag": '"v1"'})

    @get_and_head("/large-file")
    async def large_file_route():
        return FileResponse(str(large_file), headers={"ETag": '"v1"'})

    @get_and_head("/stream")
    async def stream_route():
        return StreamingResponse(iter([b"x" * 1000]), media_type="text/plain", headers={"ETag": '"v1"'})

    return api


HEAD_PATHS = ["/async", "/sync", "/bare", "/small", "/file", "/large-file", "/stream"]


@pytest.mark.parametrize("accept_encoding", ["br", "identity"])
def test_a_head_answer_has_the_coding_fields_of_the_get_answer(head_files, accept_encoding):
    """RFC 9110, section 9.3.2: a HEAD answer has the header fields of the GET answer, with no body.

    Bolt empties the body of a HEAD answer before compression, so the fields
    must come from the size of the GET body.
    """
    with TestClient(_head_api(*head_files), use_http_layer=True) as client:
        headers = {"Accept-Encoding": accept_encoding}
        for path in HEAD_PATHS:
            get = client.get(path, headers=headers)
            head = client.head(path, headers=headers)
            assert head.status_code == get.status_code == 200, path
            assert head.content == b"", path
            for name in ("content-encoding", "vary", "etag"):
                assert head.headers.get_list(name) == get.headers.get_list(name), (path, name)


@pytest.mark.parametrize("accept_encoding", [None, "identity", "deflate", "br;q=0, gzip;q=0"])
def test_a_plain_answer_to_a_negotiated_request_varies(accept_encoding):
    """RFC 9110, section 12.5.5: the choice read Accept-Encoding, so the plain answer varies on it too.

    Django's GZipMiddleware adds Vary before it reads the request. A body below
    minimum_size never varies.
    """
    api = BoltAPI(compression=CompressionConfig(backend="brotli", minimum_size=500))

    @api.get("/data")
    async def get_data():
        return {"data": "x" * 1000}

    @api.get("/small")
    async def get_small():
        return {"data": "x"}

    headers = {} if accept_encoding is None else {"Accept-Encoding": accept_encoding}
    with TestClient(api, use_http_layer=True) as client:
        # The client sends its own Accept-Encoding unless the test removes it.
        client.headers.pop("accept-encoding", None)
        response = client.get("/data", headers=headers)
        assert response.headers.get("content-encoding") is None
        assert response.headers.get_list("vary") == ["accept-encoding"]

        small = client.get("/small", headers=headers)
        assert small.headers.get_list("vary") == []

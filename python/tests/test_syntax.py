import asyncio
import json
import os
from collections.abc import AsyncIterable
from typing import Annotated

import msgspec
import pytest

from django_bolt import JSON, BoltAPI, Response, StreamingResponse
from django_bolt.exceptions import HTTPException
from django_bolt.param_functions import Cookie, Depends, Form, Header, Query
from django_bolt.param_functions import File as FileParam
from django_bolt.responses import HTML, File, FileResponse, PlainText, Redirect
from django_bolt.serializers import Serializer, field_validator
from django_bolt.testing import TestClient


@pytest.fixture(scope="module")
def api():
    """Create the test API with all routes"""
    api = BoltAPI()

    class Item(msgspec.Struct):
        name: str
        price: float
        is_offer: bool | None = None

    @api.get("/")
    async def root():
        return {"ok": True}

    @api.get("/items/{item_id}")
    async def get_item(item_id: int, q: str | None = None):
        return {"item_id": item_id, "q": q}

    @api.get("/types")
    async def get_types(b: bool | None = None, f: float | None = None):
        return {"b": b, "f": f}

    @api.put("/items/{item_id}")
    async def put_item(item_id: int, item: Item):
        return {"item_id": item_id, "item_name": item.name, "is_offer": item.is_offer}

    @api.get("/str")
    async def ret_str():
        return "hello"

    @api.get("/bytes")
    async def ret_bytes():
        return b"abc"

    @api.get("/json")
    async def ret_json():
        return JSON({"x": 1}, status_code=201, headers={"X-Test": "1"})

    @api.get("/req/{x}")
    async def req_only(req):
        return {"p": req["params"].get("x"), "q": req["query"].get("y")}

    @api.post("/m")
    async def post_m():
        return {"m": "post"}

    @api.patch("/m")
    async def patch_m():
        return {"m": "patch"}

    @api.delete("/m")
    async def delete_m():
        return {"m": "delete"}

    @api.head("/m")
    async def head_m():
        return {"m": "head"}

    # Test HEAD with query params (should work like GET)
    @api.head("/items/{item_id}")
    async def head_item(item_id: int, q: str | None = None):
        return {"item_id": item_id, "q": q}

    # Response coercion from objects to msgspec.Struct
    class Mini(msgspec.Struct):
        id: int
        username: str

    class Model:
        def __init__(self, id: int, username: str | None):
            self.id = id
            self.username = username

    @api.get("/coerce/mini", response_model=list[Mini])
    async def coerce_mini() -> list[Mini]:
        return [Model(1, "a"), Model(2, "b")]

    @api.get("/coerce/mini-bad", response_model=list[Mini])
    async def coerce_mini_bad() -> list[Mini]:
        return [Model(1, None)]

    @api.get("/ok-list", response_model=list[Item])
    async def ok_list():
        return [
            {"name": "a", "price": 1.0, "is_offer": True},
            {"name": "b", "price": 2.0, "is_offer": False},
        ]

    @api.get("/bad-list", response_model=list[Item])
    async def bad_list():
        return [{"name": "x", "is_offer": True}]

    @api.get("/anno-list")
    async def anno_list() -> list[Item]:
        return [Item(name="c", price=3.0, is_offer=None)]

    @api.get("/anno-bad")
    async def anno_bad() -> list[Item]:
        return [{"name": "d"}]

    @api.get("/both-override", response_model=list[Item])
    async def both_override() -> list[str]:
        return [{"name": "o", "price": 1.0, "is_offer": False}]

    @api.get("/no-validate")
    async def no_validate():
        return [{"anything": 1, "extra": "ok"}]

    @api.get("/status-default", status_code=201)
    async def status_default():
        return {"ok": True}

    @api.get("/headers-cookies")
    async def headers_cookies(agent: str = Depends(lambda user_agent: user_agent)):
        return {"ok": True}

    @api.get("/header")
    async def get_header(x: Annotated[str, Header(alias="x-test")]):
        return PlainText(x)

    @api.get("/cookie")
    async def get_cookie(val: Annotated[str, Cookie(alias="session")]):
        return PlainText(val)

    @api.get("/exc")
    async def raise_exc():
        raise HTTPException(418, {"detail": "teapot"}, headers={"X-Err": "1"})

    @api.get("/html")
    async def get_html():
        return HTML("<h1>Hi</h1>")

    @api.get("/redirect")
    async def get_redirect():
        return Redirect("/", status_code=302)

    THIS_FILE = os.path.abspath(__file__)

    @api.get("/file")
    async def get_file():
        return File(THIS_FILE, filename="test_syntax.py")

    @api.get("/fileresponse")
    async def get_fileresponse():
        return FileResponse(THIS_FILE, filename="test_syntax.py")

    @api.get("/stream-plain")
    async def stream_plain():
        def gen():
            for i in range(3):
                yield f"p{i},"

        return StreamingResponse(gen(), media_type="text/plain")

    @api.get("/stream-bytes")
    async def stream_bytes():
        def gen():
            for i in range(2):
                yield str(i).encode()

        return StreamingResponse(gen())

    @api.get("/sse")
    async def stream_sse():
        def gen():
            yield "event: message\ndata: hello\n\n"
            yield "data: 1\n\n"
            yield ": comment\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @api.get("/stream-async")
    async def stream_async():
        async def async_gen():
            for i in range(3):
                await asyncio.sleep(0.001)
                yield f"async-{i},".encode()

        return StreamingResponse(async_gen(), media_type="text/plain")

    @api.get("/stream-async-sse")
    async def stream_async_sse():
        async def async_gen():
            yield "event: start\ndata: beginning\n\n"
            await asyncio.sleep(0.001)
            yield "event: message\ndata: async data\n\n"
            await asyncio.sleep(0.001)
            yield "event: end\ndata: finished\n\n"

        return StreamingResponse(async_gen(), media_type="text/event-stream")

    @api.get("/stream-async-large")
    async def stream_async_large():
        async def async_gen():
            for i in range(10):
                await asyncio.sleep(0.001)
                chunk = f"chunk-{i:02d}-{'x' * 100}\n".encode()
                yield chunk

        return StreamingResponse(async_gen(), media_type="application/octet-stream")

    @api.get("/stream-async-mixed-types")
    async def stream_async_mixed_types():
        async def async_gen():
            yield b"bytes-chunk\n"
            await asyncio.sleep(0.001)
            yield "string-chunk\n"
            await asyncio.sleep(0.001)
            yield bytearray(b"bytearray-chunk\n")
            await asyncio.sleep(0.001)
            yield memoryview(b"memoryview-chunk\n")

        return StreamingResponse(async_gen(), media_type="text/plain")

    @api.get("/stream-items-typed")
    async def stream_items_typed() -> AsyncIterable[Item]:
        async def gen():
            for i in range(3):
                await asyncio.sleep(0.001)
                yield Item(name=f"item-{i}", price=float(i), is_offer=(i % 2 == 0))

        return gen()

    @api.get("/stream-async-error")
    async def stream_async_error():
        async def async_gen():
            yield b"chunk1\n"
            await asyncio.sleep(0.001)
            yield b"chunk2\n"
            await asyncio.sleep(0.001)
            raise ValueError("Simulated async error")

        return StreamingResponse(async_gen(), media_type="text/plain")

    @api.post("/form-urlencoded")
    async def form_urlencoded(a: Annotated[str, Form()], b: Annotated[int, Form()]):
        return {"a": a, "b": b}

    # Form data with Struct/Serializer
    class FormDataStruct(msgspec.Struct):
        username: str
        age: int
        active: bool = True

    @api.post("/form-struct")
    async def form_struct(data: Annotated[FormDataStruct, Form()]):
        return {"username": data.username, "age": data.age, "active": data.active}

    # Form data with Serializer and field_validator
    class FormDataSerializer(Serializer):
        username: str
        email: str

        @field_validator("username")
        def validate_username(cls, value):
            if len(value) < 3:
                raise ValueError("Username must be at least 3 characters")
            return value

    @api.post("/form-serializer")
    async def form_serializer(data: Annotated[FormDataSerializer, Form()]):
        return {"username": data.username, "email": data.email}

    # Query parameters with Struct
    class QueryParams(msgspec.Struct):
        limit: int = 10
        offset: int = 0
        search: str | None = None

    @api.get("/query-struct")
    async def query_struct(params: Annotated[QueryParams, Query()]):
        return {"limit": params.limit, "offset": params.offset, "search": params.search}

    # Query parameters with Struct + explicit field names (aliases)
    class ReportFilters(msgspec.Struct):
        category_id: int | None = msgspec.field(default=None, name="categoryID")
        min_price: float | None = msgspec.field(default=None, name="minPrice")
        query: str | None = None

    @api.get("/query-struct-rename")
    async def query_struct_rename(filters: Annotated[ReportFilters, Query()]):
        return {"category_id": filters.category_id, "min_price": filters.min_price, "query": filters.query}

    # Query parameters with Struct + class-level rename
    class CamelExample(msgspec.Struct, rename="camel"):
        field_one: int
        field_two: str

    @api.get("/query-struct-rename-camel")
    async def query_struct_rename_camel(example: Annotated[CamelExample, Query()]):
        return {"field_one": example.field_one, "field_two": example.field_two}

    # Header parameters with Struct
    class HeaderParams(msgspec.Struct):
        x_api_key: str
        x_request_id: str | None = None

    @api.get("/header-struct")
    async def header_struct(headers: Annotated[HeaderParams, Header()]):
        return {"api_key": headers.x_api_key, "request_id": headers.x_request_id}

    # Cookie parameters with Struct
    class CookieParams(msgspec.Struct):
        session_id: str
        theme: str = "light"

    @api.get("/cookie-struct")
    async def cookie_struct(cookies: Annotated[CookieParams, Cookie()]):
        return {"session_id": cookies.session_id, "theme": cookies.theme}

    @api.post("/upload")
    async def upload(files: Annotated[list[dict], FileParam(alias="file")]):
        return {"count": len(files), "names": [f.get("filename") for f in files]}

    @api.get("/sse-async-test")
    async def sse_async_test():
        async def agen():
            for i in range(3):
                yield f"data: {i}\n\n"
                await asyncio.sleep(0)

        return StreamingResponse(agen(), media_type="text/event-stream")

    @api.post("/v1/chat/completions-async-test")
    async def chat_completions_async_test(payload: dict):
        if payload.get("stream", True):

            async def agen():
                for i in range(payload.get("n_chunks", 2)):
                    data = {"chunk": i, "content": " hello"}
                    yield f"data: {json.dumps(data)}\n\n"
                    await asyncio.sleep(0)
                yield "data: [DONE]\n\n"

            return StreamingResponse(agen(), media_type="text/event-stream")
        return {"non_streaming": True}

    # 204 No Content - returning None should work
    @api.delete("/no-content", status_code=204)
    async def no_content():
        return None

    # Returning None without 204 should error
    @api.get("/none-return")
    async def none_return():
        return None

    return api


@pytest.fixture(scope="module")
def client(api):
    """Create TestClient for the API"""
    with TestClient(api) as client:
        yield client


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_path_and_query_binding(client):
    response = client.get("/items/42?q=hello")
    assert response.status_code == 200
    assert response.json() == {"item_id": 42, "q": "hello"}


def test_bool_and_float_binding(client):
    response = client.get("/types?b=true&f=1.25")
    assert response.status_code == 200
    assert response.json() == {"b": True, "f": 1.25}


def test_body_decoding(client):
    response = client.put("/items/5", json={"name": "x", "price": 1.5, "is_offer": True})
    assert response.status_code == 200
    assert response.json() == {"item_id": 5, "item_name": "x", "is_offer": True}


def test_response_types(client):
    # str
    response = client.get("/str")
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers.get("content-type", "").startswith("text/plain")
    # bytes
    response = client.get("/bytes")
    assert response.status_code == 200
    assert response.content == b"abc"
    assert response.headers.get("content-type", "").startswith("application/octet-stream")


def test_json_response_status_and_headers(client):
    response = client.get("/json")
    assert response.status_code == 201
    assert response.headers.get("x-test") == "1"
    assert response.json() == {"x": 1}


def test_request_only_handler(client):
    response = client.get("/req/9?y=z")
    assert response.status_code == 200
    assert response.json() == {"p": "9", "q": "z"}


def test_methods(client):
    response = client.post("/m")
    assert response.status_code == 200 and response.json() == {"m": "post"}
    response = client.patch("/m")
    assert response.status_code == 200 and response.json() == {"m": "patch"}
    response = client.delete("/m")
    assert response.status_code == 200 and response.json() == {"m": "delete"}


def test_response_model_validation_ok(client):
    response = client.get("/ok-list")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list) and len(data) == 2
    assert data[0]["name"] == "a" and data[0]["price"] == 1.0


def test_response_model_validation_error(client):
    response = client.get("/bad-list")
    # We currently surface server error (500) on validation problems
    assert response.status_code == 500
    assert b"Response validation error" in response.content


def test_return_annotation_validation_ok(client):
    response = client.get("/anno-list")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list) and data[0]["name"] == "c"


def test_return_annotation_validation_error(client):
    response = client.get("/anno-bad")
    assert response.status_code == 500
    assert b"Response validation error" in response.content


def test_response_coercion_from_objects(client):
    response = client.get("/coerce/mini")
    assert response.status_code == 200
    data = response.json()
    assert data == [{"id": 1, "username": "a"}, {"id": 2, "username": "b"}]


def test_response_coercion_error_from_objects(client):
    response = client.get("/coerce/mini-bad")
    assert response.status_code == 500
    assert b"Response validation error" in response.content


def test_response_model_overrides_return_annotation(client):
    response = client.get("/both-override")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list) and data[0]["name"] == "o"


def test_no_validation_without_types(client):
    response = client.get("/no-validate")
    assert response.status_code == 200
    data = response.json()
    # Should return as-is since neither annotation nor response_model provided
    assert data == [{"anything": 1, "extra": "ok"}]


def test_status_code_default(client):
    response = client.get("/status-default")
    assert response.status_code == 201


def test_header_and_cookie(client):
    response = client.get("/header", headers={"x-test": "val"})
    assert response.status_code == 200 and response.content == b"val"
    # set cookie via header
    response = client.get("/cookie", cookies={"session": "abc"})
    assert response.status_code == 200 and response.content == b"abc"


def test_http_exception(client):
    response = client.get("/exc")
    assert response.status_code == 418
    assert response.headers.get("x-err") == "1"


def test_response_helpers(client):
    response = client.get("/html")
    assert response.status_code == 200 and response.headers.get("content-type", "").startswith("text/html")
    response = client.get("/redirect", follow_redirects=False)
    assert response.status_code == 302 and response.headers.get("location") == "/"
    response = client.get("/file")
    assert response.status_code == 200 and response.headers.get("content-type", "").startswith("text/")
    # FileResponse should also succeed and set content-disposition
    response = client.get("/fileresponse")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/")
    assert "attachment;" in (response.headers.get("content-disposition", "").lower())


def test_streaming_plain(client):
    response = client.get("/stream-plain")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/plain")
    assert response.content == b"p0,p1,p2,"


def test_streaming_bytes_default_content_type(client):
    response = client.get("/stream-bytes")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/octet-stream")
    assert response.content == b"01"


def test_streaming_sse_headers(client):
    response = client.get("/sse")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/event-stream")
    # SSE-friendly headers are set by the server
    # Note: Connection header may be managed by the HTTP server automatically
    assert response.headers.get("x-accel-buffering", "").lower() == "no"
    # Body should contain multiple well-formed SSE lines
    text = response.content.decode()
    assert "event: message" in text
    assert "data: hello" in text
    assert "data: 1" in text
    assert ": comment" in text


def test_streaming_async_large(client):
    """Test async streaming with larger payloads."""
    response = client.get("/stream-async-large")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/octet-stream")

    # Should have 10 chunks
    lines = response.content.decode().strip().split("\n")
    assert len(lines) == 10

    # Check format of chunks
    for i, line in enumerate(lines):
        expected_prefix = f"chunk-{i:02d}-"
        assert line.startswith(expected_prefix)
        assert len(line) >= 109  # ~109 bytes per line (110 bytes per chunk with \n)
        assert line.endswith("x" * 100)


def test_streaming_async_mixed_types(client):
    """Test async streaming with different data types."""
    response = client.get("/stream-async-mixed-types")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/plain")

    # Check all data types are properly converted
    text = response.content.decode()
    expected_chunks = ["bytes-chunk\n", "string-chunk\n", "bytearray-chunk\n", "memoryview-chunk\n"]

    for expected in expected_chunks:
        assert expected in text


def test_streaming_typed_async_iterable_ndjson(client):
    response = client.get("/stream-items-typed")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/x-ndjson")

    lines = [line for line in response.content.splitlines() if line]
    assert len(lines) == 3
    payload = [json.loads(line) for line in lines]

    assert payload[0] == {"name": "item-0", "price": 0.0, "is_offer": True}
    assert payload[1] == {"name": "item-1", "price": 1.0, "is_offer": False}
    assert payload[2] == {"name": "item-2", "price": 2.0, "is_offer": True}


def test_streaming_async_vs_sync_compatibility(client):
    """Test that async and sync streaming produce the same results for equivalent data."""

    # Get sync streaming result
    sync_response = client.get("/stream-plain")

    # Get async streaming result
    async_response = client.get("/stream-async")

    # Both should succeed
    assert sync_response.status_code == 200
    assert async_response.status_code == 200

    # Both should be text/plain
    assert sync_response.headers.get("content-type", "").startswith("text/plain")
    assert async_response.headers.get("content-type", "").startswith("text/plain")

    # Content should be similar format (both have 3 items)
    sync_text = sync_response.content.decode()
    async_text = async_response.content.decode()

    # Both should have 3 comma-separated items
    assert len(sync_text.split(",")) == 4  # "p0,p1,p2," = 4 parts
    assert len(async_text.split(",")) == 4  # "async-0,async-1,async-2," = 4 parts


def test_async_bridge_endpoints_work(client):
    """Test that async SSE streaming works correctly."""

    # Test the async SSE endpoint - this should expose the real bug
    response = client.get("/sse-async-test")
    assert response.status_code == 200, f"Async SSE endpoint failed with status {response.status_code}"
    assert len(response.content) > 0, f"Async SSE endpoint returned empty body, got {len(response.content)} bytes"
    # Check that we actually get SSE formatted data
    text = response.content.decode()
    assert "data: 0" in text, f"Expected SSE data not found in response: {text[:100]}"
    assert "data: 1" in text, f"Expected SSE data not found in response: {text[:100]}"


def test_streaming_requires_generator_instance():
    """Test that StreamingResponse requires a generator instance, not a function.

    Both sync and async generators must be called with () before passing to StreamingResponse.
    This standardizes the API and ensures proper async/sync detection at instantiation time.
    """

    # Test 1: Sync generator function without () should raise TypeError
    def gen():
        yield "data"

    with pytest.raises(TypeError, match="Call your generator function with parentheses"):
        StreamingResponse(gen, media_type="text/plain")

    # Test 2: Async generator function without () should raise TypeError
    async def agen():
        yield "data"

    with pytest.raises(TypeError, match="Call your generator function with parentheses"):
        StreamingResponse(agen, media_type="text/event-stream")

    # Test 3: Non-generator callable should also raise TypeError
    def not_a_generator():
        return "data"

    with pytest.raises(TypeError, match="content must be a generator instance"):
        StreamingResponse(not_a_generator, media_type="text/plain")

    # Test 4: Valid sync generator with () should work
    def gen2():
        for i in range(2):
            yield f"item{i},"

    response = StreamingResponse(gen2(), media_type="text/plain")
    assert response.is_async_generator is False

    # Test 5: Valid async generator with () should work
    async def agen2():
        for i in range(2):
            yield f"data: {i}\n\n"

    response2 = StreamingResponse(agen2(), media_type="text/event-stream")
    assert response2.is_async_generator is True


def test_form_and_file(client):
    response = client.post("/form-urlencoded", data={"a": "x", "b": "3"})
    assert response.status_code == 200 and response.json() == {"a": "x", "b": 3}

    # Test multipart file upload
    response = client.post(
        "/upload",
        data={"note": "hi"},
        files=[
            ("file", ("a.txt", b"abc", "application/octet-stream")),
            ("file", ("b.txt", b"def", "application/octet-stream")),
        ],
    )
    data = response.json()
    assert response.status_code == 200 and data["count"] == 2 and set(data["names"]) == {"a.txt", "b.txt"}


def test_large_file_upload_rejected_by_default(api):
    """Test that large file uploads are rejected with default 1MB limit.

    Note: Upload size validation is now handled in Rust (form_parsing.rs).
    The default max_upload_size is 1MB, set in middleware/compiler.py.
    """
    with TestClient(api, use_http_layer=True) as test_client:
        # Create a 2MB file content (exceeds default 1MB limit)
        large_content = b"x" * (2 * 1024 * 1024)  # 2MB

        response = test_client.post(
            "/upload", files=[("file", ("large.bin", large_content, "application/octet-stream"))]
        )

        # Should fail with 422 (validation error) because file exceeds default 1MB limit
        # Rust returns 422 for file size validation errors
        assert response.status_code in (413, 422), f"Expected 413 or 422, got {response.status_code}: {response.text}"


def test_large_file_upload_with_increased_limit(api):
    """Test that file uploads within the limit work correctly.

    Note: Upload size limits are now compiled into route metadata at startup.
    The default max_upload_size is 1MB, set in middleware/compiler.py.
    Testing dynamic BOLT_MAX_UPLOAD_SIZE changes requires server restart.
    """
    import pytest

    # This test is skipped because max_upload_size is now compiled at route registration time
    # and cannot be changed dynamically. To test increased limits, need to configure
    # max_upload_size in route metadata before server starts.
    pytest.skip("Upload size limits are now compiled at route registration time (Rust implementation)")


def test_error_responses_have_cors_headers(api):
    """Test that error responses (404, etc.) include CORS headers.

    Note: Actix-level errors (like PayloadConfig 413) happen before the handler runs
    and don't include CORS headers. The fix is to set BOLT_MAX_UPLOAD_SIZE high enough
    that requests pass through to Python, where errors include CORS headers.
    """
    # Use HTTP layer with CORS enabled
    with TestClient(api, use_http_layer=True, cors_allowed_origins=["*"]) as test_client:
        response = test_client.post(
            "/nonexistent",
            content=b"x" * 1000,
            headers={"Origin": "http://example.com", "Content-Type": "application/json"},
        )

        # 404 errors should have CORS headers
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        assert "access-control-allow-origin" in response.headers, (
            f"Missing CORS header on 404. Headers: {dict(response.headers)}"
        )


def test_head_method(client):
    """Test HEAD method works correctly"""
    response = client.head("/m")
    assert response.status_code == 200
    # HEAD should return headers but empty body
    assert len(response.content) == 0


def test_head_with_params(client):
    """Test HEAD method with path and query params"""
    response = client.head("/items/42?q=test")
    assert response.status_code == 200


def test_options_method_automatic(client):
    """Test automatic OPTIONS handling - returns Allow header with available methods"""
    response = client.options("/m")
    # 204 No Content is the standard response for OPTIONS preflight
    assert response.status_code == 204
    # Check Allow header is present and contains the methods
    assert "allow" in response.headers or "Allow" in response.headers
    allow_header = response.headers.get("allow") or response.headers.get("Allow")
    assert allow_header is not None
    # Should include all methods registered for /m (POST, PATCH, DELETE, HEAD)
    methods = [m.strip() for m in allow_header.split(",")]
    assert "POST" in methods
    assert "PATCH" in methods
    assert "DELETE" in methods
    assert "HEAD" in methods
    assert "OPTIONS" in methods  # Always included for automatic OPTIONS
    # 204 has no body content


def test_options_on_nonexistent_route(client):
    """Test OPTIONS on non-existent route returns 404"""
    response = client.options("/does-not-exist")
    assert response.status_code == 404


def test_explicit_options_handler():
    """Test that explicit OPTIONS handler overrides automatic behavior"""
    api = BoltAPI()

    @api.get("/custom-options")
    async def get_custom():
        return {"result": "data"}

    @api.options("/custom-options")
    async def options_custom():
        return Response(
            {"custom": "options", "info": "This is a custom OPTIONS handler"},
            headers={"Allow": "GET, OPTIONS", "X-Custom": "header"},
        )

    with TestClient(api) as client:
        response = client.options("/custom-options")
        assert response.status_code == 200
        data = response.json()
        assert data["custom"] == "options"
        assert data["info"] == "This is a custom OPTIONS handler"
        # Check custom headers
        assert "allow" in response.headers or "Allow" in response.headers
        assert "x-custom" in response.headers or "X-Custom" in response.headers


def test_method_validation():
    """Test that HEAD and OPTIONS don't accept body parameters"""
    api = BoltAPI()

    class Body(msgspec.Struct):
        value: str

    # HEAD should not accept body
    with pytest.raises(TypeError, match="HEAD.*cannot have body parameters"):

        @api.head("/test-head")
        async def head_with_body(body: Body):
            return {"ok": True}

    # OPTIONS should not accept body
    with pytest.raises(TypeError, match="OPTIONS.*cannot have body parameters"):

        @api.options("/test-options")
        async def options_with_body(body: Body):
            return {"ok": True}


def test_204_no_content_with_none_return(client):
    """Test that returning None with status_code=204 returns empty response."""
    response = client.delete("/no-content")
    assert response.status_code == 204
    assert response.content == b""


def test_none_return_without_204_returns_500(client):
    """Test that returning None without 204 status code returns 500 error."""
    response = client.get("/none-return")
    assert response.status_code == 500


def test_form_struct(client):
    """Test Form() with Struct type parses form data into struct."""
    response = client.post("/form-struct", data={"username": "john", "age": "30", "active": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "john"
    assert data["age"] == 30
    assert data["active"] is True


def test_form_struct_with_defaults(client):
    """Test Form() with Struct uses default values."""
    response = client.post(
        "/form-struct",
        data={"username": "jane", "age": "25"},  # active not provided, uses default
    )
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "jane"
    assert data["age"] == 25
    assert data["active"] is True  # default value


def test_form_struct_missing_required(client):
    """Test Form() with Struct returns 422 for missing required fields."""
    response = client.post(
        "/form-struct",
        data={"username": "john"},  # missing age
    )
    assert response.status_code == 422


def test_form_serializer(client):
    """Test Form() with Serializer parses and validates form data."""
    response = client.post("/form-serializer", data={"username": "john", "email": "john@example.com"})
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "john"
    assert data["email"] == "john@example.com"


def test_form_serializer_validation_error(client):
    """Test Form() with Serializer runs field_validator and returns 422."""
    response = client.post(
        "/form-serializer",
        data={"username": "ab", "email": "ab@example.com"},  # username too short
    )
    assert response.status_code == 422


def test_query_struct(client):
    """Test Query() with Struct type parses query parameters into struct."""
    response = client.get("/query-struct?limit=20&offset=5&search=test")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 20
    assert data["offset"] == 5
    assert data["search"] == "test"


def test_query_struct_with_defaults(client):
    """Test Query() with Struct uses default values."""
    response = client.get("/query-struct")  # no params, uses defaults
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert data["search"] is None


def test_query_struct_rename_accepts_alias(client):
    """Test Query() struct accepts msgspec field name (alias)."""
    response = client.get("/query-struct-rename?categoryID=7&minPrice=3.5&query=bolt")
    assert response.status_code == 200
    data = response.json()
    assert data["category_id"] == 7
    assert data["min_price"] == 3.5
    assert data["query"] == "bolt"


def test_query_struct_rename_camel_accepts_alias(client):
    """Test Query() struct accepts camelCase alias from rename='camel'."""
    response = client.get("/query-struct-rename-camel?fieldOne=1&fieldTwo=hello")
    assert response.status_code == 200
    data = response.json()
    assert data["field_one"] == 1
    assert data["field_two"] == "hello"


def test_header_struct(client):
    """Test Header() with Struct type parses headers into struct."""
    response = client.get("/header-struct", headers={"X-Api-Key": "secret123", "X-Request-Id": "req-456"})
    assert response.status_code == 200
    data = response.json()
    assert data["api_key"] == "secret123"
    assert data["request_id"] == "req-456"


def test_header_struct_missing_required(client):
    """Test Header() with Struct returns 422 for missing required headers."""
    response = client.get("/header-struct")  # missing X-Api-Key
    assert response.status_code == 422


def test_cookie_struct(client):
    """Test Cookie() with Struct type parses cookies into struct."""
    response = client.get("/cookie-struct", cookies={"session_id": "abc123", "theme": "dark"})
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "abc123"
    assert data["theme"] == "dark"


def test_cookie_struct_with_defaults(client):
    """Test Cookie() with Struct uses default values."""
    response = client.get(
        "/cookie-struct",
        cookies={"session_id": "xyz789"},  # theme not provided, uses default
    )
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "xyz789"
    assert data["theme"] == "light"  # default value

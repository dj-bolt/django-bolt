"""Auto-streaming of handler results, and the data handlers for raw results.

A handler that returns a generator gets a streamed response. An object that
is not a generator streams only under a stream return annotation. Bolt
validates each item of a typed stream and sends the stream as NDJSON.

Each test builds its own BoltAPI, sends a request, and checks the status,
the headers and the full body. TestClient reads a streamed body in full.
"""

from __future__ import annotations

import asyncio
import json
import typing
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError
from django_bolt.responses import JSON
from django_bolt.testing import TestClient

NDJSON = "application/x-ndjson"
OCTET_STREAM = "application/octet-stream"
UNSUPPORTED_INT = (
    "Handler returned unsupported type 'int'. "
    "Return dict, list, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
)


class Item(msgspec.Struct):
    id: int
    note: str = "default"


def _echo_exception(error: BoltError, request) -> JSON:
    """Put the kind of the error and the message of its exception in the body."""
    return JSON({"kind": error.kind, "exception": str(error.exception)}, status_code=error.status)


def _ndjson(response) -> list[dict]:
    """Return the lines of an NDJSON body as objects."""
    assert response.headers["content-type"] == NDJSON, response.text
    return [json.loads(line) for line in response.content.splitlines()]


class Chunks:
    """An iterable with only ``__iter__``."""

    def __iter__(self):
        return iter([b"a", b"b"])


class AsyncChunks:
    """An async iterable with only ``__aiter__``."""

    def __aiter__(self):
        return self._chunks()

    async def _chunks(self):
        yield b"a"
        yield b"b"


class AnextOnly:
    """An async iterator with only ``__anext__``."""

    def __init__(self):
        self._chunks = [b"a", b"b"]

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class NextOnly:
    """An iterator with only ``__next__``."""

    def __init__(self):
        self._chunks = [b"a", b"b"]

    def __next__(self):
        if not self._chunks:
            raise StopIteration
        return self._chunks.pop(0)


class Rows:
    """An async iterator that is not an async generator. It gives one row."""

    def __init__(self):
        self._rows = [{"id": 1, "secret": "x"}]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


# --- Results that do not stream -------------------------------------------


def test_plain_iterator_is_not_streamed_without_a_stream_annotation():
    api = BoltAPI(error_formatter=_echo_exception)

    @api.get("/untyped")
    def untyped():
        return iter([b"a"])

    @api.get("/optional")
    def optional() -> Item | None:
        return iter([b"a"])

    @api.get("/struct")
    def struct() -> Item:
        return iter([b"a"])

    with TestClient(api) as client:
        response = client.get("/untyped")
        assert response.status_code == 500, response.text
        assert response.json()["exception"].startswith("Handler returned unsupported type 'list_iterator'.")

        for path in ("/optional", "/struct"):
            response = client.get(path)
            assert response.status_code == 500, (path, response.text)
            assert response.json()["kind"] == "response_validation", path


def test_tuple_is_not_streamed_under_a_stream_annotation():
    api = BoltAPI()

    @api.get("/tuple")
    def chunks_tuple() -> Iterator[bytes]:
        return (b"a", b"b")

    with TestClient(api) as client:
        response = client.get("/tuple")

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["detail"] == "Response validation error"
    assert "Expected `Iterator`, got `tuple`" in body["extra"]["validation_errors"][0]["msg"]


# --- Raw streams ----------------------------------------------------------


def test_plain_iterator_streams_raw_under_a_bare_or_bytes_stream_annotation():
    api = BoltAPI()

    @api.get("/bare")
    def bare() -> Iterator:
        return iter([b"a", b"b"])

    # The bare typing alias has an origin but no item type.
    @api.get("/typing-bare")
    def typing_bare() -> typing.Iterator:
        return iter([b"a", b"b"])

    @api.get("/bytes")
    def chunks_bytes() -> Iterator[bytes]:
        return iter([b"a", b"b"])

    with TestClient(api) as client:
        for path in ("/bare", "/typing-bare", "/bytes"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"] == OCTET_STREAM, path
            assert response.content == b"ab", path


@pytest.mark.parametrize(
    "path",
    ["/iterable", "/async-iterable", "/anext-only", "/next-only"],
)
def test_stream_protocol_object_streams_under_a_stream_annotation(path):
    api = BoltAPI()

    @api.get("/iterable")
    def iterable() -> Iterable[bytes]:
        return Chunks()

    @api.get("/async-iterable")
    def async_iterable() -> AsyncIterable[bytes]:
        return AsyncChunks()

    @api.get("/anext-only")
    def anext_only() -> AsyncIterator[bytes]:
        return AnextOnly()

    @api.get("/next-only")
    def next_only() -> Iterator[bytes]:
        return NextOnly()

    with TestClient(api) as client:
        response = client.get(path)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == OCTET_STREAM
    assert response.content == b"ab"


def test_untyped_sync_generator_streams_raw_with_the_route_status():
    api = BoltAPI()

    @api.get("/gen", status_code=201)
    def gen():
        yield b"a"
        yield b"b"

    with TestClient(api) as client:
        response = client.get("/gen")

    assert response.status_code == 201, response.text
    assert response.headers["content-type"] == OCTET_STREAM
    assert response.content == b"ab"


# --- Typed streams --------------------------------------------------------


def test_typed_sync_generator_streams_validated_ndjson_with_the_route_status():
    api = BoltAPI()

    @api.get("/items", status_code=201)
    def items() -> Iterator[Item]:
        yield {"id": 1, "secret": "x"}
        yield Item(id=2)

    with TestClient(api) as client:
        response = client.get("/items")

    assert response.status_code == 201, response.text
    assert response.headers["content-type"] == NDJSON
    assert response.content == b'{"id":1,"note":"default"}\n{"id":2,"note":"default"}\n'


def test_typed_async_iterator_that_is_not_a_generator_streams_validated_ndjson():
    api = BoltAPI()

    @api.get("/rows")
    async def rows() -> AsyncIterator[Item]:
        await asyncio.sleep(0)
        return Rows()

    with TestClient(api) as client:
        response = client.get("/rows")

    assert response.status_code == 200, response.text
    assert _ndjson(response) == [{"id": 1, "note": "default"}]


def test_typed_stream_items_pass_unvalidated_when_response_validation_is_off():
    api = BoltAPI()

    @api.get("/raw", validate_response=False)
    def raw() -> Iterator[Item]:
        yield {"id": 1, "secret": "x"}

    with TestClient(api) as client:
        response = client.get("/raw")

    assert response.status_code == 200, response.text
    assert _ndjson(response) == [{"id": 1, "secret": "x"}]


def test_optional_stream_annotation_with_none_first_streams_validated_ndjson():
    api = BoltAPI()

    @api.get("/maybe")
    def maybe() -> None | Iterator[Item]:
        yield {"id": 1}

    with TestClient(api) as client:
        response = client.get("/maybe")

    assert response.status_code == 200, response.text
    assert _ndjson(response) == [{"id": 1, "note": "default"}]


# --- Raw results of an untyped handler ------------------------------------


@pytest.fixture
def raw_client():
    api = BoltAPI(error_formatter=_echo_exception)

    # Each async handler awaits once. Thus the async data handler serializes
    # its result, not the sync one.
    @api.get("/bytes/async")
    async def bytes_async():
        await asyncio.sleep(0)
        return b"\x00\x01"

    @api.get("/bytes/sync")
    def bytes_sync():
        return b"\x00\x01"

    @api.get("/text/async")
    async def text_async():
        await asyncio.sleep(0)
        return "hello"

    @api.get("/none/async")
    async def none_async():
        await asyncio.sleep(0)
        return None

    @api.get("/int/async")
    async def int_async():
        await asyncio.sleep(0)
        return 42

    @api.get("/int/sync")
    def int_sync():
        return 42

    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize("path", ["/bytes/async", "/bytes/sync"])
def test_bytes_result_is_the_exact_octet_stream_body(raw_client, path):
    response = raw_client.get(path)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == OCTET_STREAM
    assert response.content == b"\x00\x01"


def test_str_result_of_an_async_handler_is_a_plain_text_body(raw_client):
    response = raw_client.get("/text/async")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.content == b"hello"


def test_none_result_of_an_async_handler_is_an_error_when_the_status_is_not_204(raw_client):
    response = raw_client.get("/none/async")

    assert response.status_code == 500, response.text
    assert response.json()["exception"].startswith("Handler returned unsupported type 'NoneType'.")


@pytest.mark.parametrize("path", ["/int/async", "/int/sync"])
def test_unsupported_result_is_an_error_that_names_its_type(raw_client, path):
    response = raw_client.get(path)

    assert response.status_code == 500, response.text
    assert response.json() == {"kind": "internal_error", "exception": UNSUPPORTED_INT}

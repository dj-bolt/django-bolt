"""Content-type headers emitted for Response objects.

Response builds its wire meta directly from the media type. Media types that
match a static Rust content-type use an integer meta tag instead of a tuple.
These tests pin the header that each path emits. They fail if the static map
in serialization.py drifts from ResponseType::content_type() in Rust.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt import JSON, BoltAPI
from django_bolt.responses import File, FileResponse, PlainText, Redirect, Response
from django_bolt.testing import TestClient

# Media types that take the integer-tag path, and the header Rust must emit.
STATIC_MEDIA_TYPES = [
    "application/json",
    "application/octet-stream",
    "text/plain; charset=utf-8",
    "text/html; charset=utf-8",
]

# Media types that take the meta-tuple path.
TUPLE_MEDIA_TYPES = [
    "text/plain",
    "text/html",
    "application/vnd.api+json",
    "text/csv",
]

ALL_MEDIA_TYPES = STATIC_MEDIA_TYPES + TUPLE_MEDIA_TYPES


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    for index, media_type in enumerate(ALL_MEDIA_TYPES):

        def make(media_type=media_type):
            async def handler():
                return Response(b"body", media_type=media_type)

            return handler

        api.get(f"/media/{index}")(make())

    @api.get("/custom-header")
    async def custom_header():
        return Response(b"body", media_type="application/json", headers={"X-Custom": "1"})

    @api.get("/override")
    async def override():
        return Response(b"body", media_type="application/json", headers={"Content-Type": "text/csv"})

    @api.get("/override-lowercase")
    async def override_lowercase():
        return Response(b"body", media_type="application/json", headers={"content-type": "text/csv"})

    @api.get("/with-cookie")
    async def with_cookie():
        return Response(b"body", media_type="application/json").set_cookie("session", "abc")

    with TestClient(api) as test_client:
        yield test_client


@pytest.mark.parametrize("media_type", ALL_MEDIA_TYPES)
def test_media_type_becomes_the_content_type(client, media_type):
    """The media type is sent verbatim, on both the static and the tuple path."""
    response = client.get(f"/media/{ALL_MEDIA_TYPES.index(media_type)}")
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.content == b"body"


def test_custom_header_keeps_the_media_type(client):
    """A custom header does not replace the content-type."""
    response = client.get("/custom-header")
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-custom"] == "1"


@pytest.mark.parametrize("path", ["/override", "/override-lowercase"])
def test_content_type_header_overrides_the_media_type(client, path):
    """A content-type header wins over the media type, in any letter case."""
    response = client.get(path)
    assert response.headers["content-type"] == "text/csv"


def test_cookie_keeps_the_media_type(client):
    """A cookie does not replace the content-type."""
    response = client.get("/with-cookie")
    assert response.headers["content-type"] == "application/json"
    assert "session=abc" in response.headers["set-cookie"]


@pytest.mark.parametrize(("path", "content_type"), [("/json", "application/json"), ("/csv", "text/csv")])
def test_custom_header_keeps_the_content_type_of_json_and_response(path, content_type):
    """Rust does not know text/csv, so only the media type can give that content type."""
    api = BoltAPI()

    @api.get("/json")
    async def json_route():
        return JSON({"a": 1}, headers={"X-Extra": "1"})

    @api.get("/csv")
    async def csv_route():
        return Response(b"x", media_type="text/csv", headers={"X-Extra": "1"})

    with TestClient(api) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["x-extra"] == "1"


def test_awaiting_handler_sends_plain_text():
    """The handler awaits, so it takes the async serializer."""
    api = BoltAPI()

    @api.get("/text")
    async def text():
        await asyncio.sleep(0)
        return PlainText("hi")

    with TestClient(api) as client:
        response = client.get("/text")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.content == b"hi"


@pytest.mark.parametrize("path", ["/async", "/sync"])
def test_redirect_from_an_awaiting_and_a_sync_handler(path):
    """The awaiting handler takes the async serializer, the sync handler the sync one."""
    api = BoltAPI()

    @api.get("/async")
    async def async_redirect():
        await asyncio.sleep(0)
        return Redirect("/a")

    @api.get("/sync")
    def sync_redirect():
        return Redirect("/a")

    with TestClient(api) as client:
        response = client.get(path, follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/a"


@pytest.mark.parametrize("response_class", [File, FileResponse])
def test_awaiting_handler_sends_a_file(tmp_path, response_class):
    """The handler awaits, so it takes the async serializer."""
    report = tmp_path / "report.txt"
    report.write_bytes(b"report")
    api = BoltAPI()

    @api.get("/report")
    async def get_report():
        await asyncio.sleep(0)
        return response_class(str(report))

    with TestClient(api) as client:
        response = client.get("/report")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain"
    assert response.content == b"report"

"""Content-type headers emitted for Response objects.

Response builds its wire meta directly from the media type. Media types that
match a static Rust content-type use an integer meta tag instead of a tuple.
These tests pin the header that each path emits. They fail if the static map
in serialization.py drifts from ResponseType::content_type() in Rust.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.responses import Response
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

"""Payload-size limit enforcement across multipart and non-multipart requests.

The handler enforces ``BOLT_MAX_UPLOAD_SIZE`` (``state.max_payload_size``)
uniformly: a Content-Length over the limit is fast-rejected with 413 before the
body is read, on BOTH the multipart and non-multipart paths. Multipart also
bounds the aggregate bytes across all parts during streaming (for chunked
requests with no Content-Length), while per-file ``File(max_size=...)`` remains
the finer-grained limit.

These tests set a small ``BOLT_MAX_UPLOAD_SIZE`` so the limit is easy to cross.
The TestClient reads the setting when the app is constructed, so it must be set
before ``TestClient(api)``.
"""

from __future__ import annotations

import contextlib

import msgspec
import pytest
from django.conf import settings

from django_bolt import BoltAPI, UploadFile
from django_bolt.params import File
from django_bolt.testing import TestClient

# Small enough to cross easily, large enough that a tiny multipart body fits.
MAX_PAYLOAD = 2000


class Payload(msgspec.Struct):
    blob: str


@contextlib.contextmanager
def _max_payload(size: int):
    """Temporarily set BOLT_MAX_UPLOAD_SIZE, restoring the prior state."""
    had_attr = hasattr(settings, "BOLT_MAX_UPLOAD_SIZE")
    prev = getattr(settings, "BOLT_MAX_UPLOAD_SIZE", None)
    settings.BOLT_MAX_UPLOAD_SIZE = size
    try:
        yield
    finally:
        if had_attr:
            settings.BOLT_MAX_UPLOAD_SIZE = prev
        else:
            with contextlib.suppress(AttributeError):
                delattr(settings, "BOLT_MAX_UPLOAD_SIZE")


@pytest.fixture
def client():
    with _max_payload(MAX_PAYLOAD):
        api = BoltAPI()

        @api.post("/json")
        async def json_body(payload: Payload):
            return {"len": len(payload.blob)}

        @api.post("/upload")
        async def upload(avatar: UploadFile = File()):
            return {"size": avatar.size}

        with TestClient(api) as c:
            yield c


class TestNonMultipartPayloadLimit:
    def test_under_limit_succeeds(self, client):
        response = client.post("/json", json={"blob": "x" * 10})
        assert response.status_code == 200
        assert response.json()["len"] == 10

    def test_over_limit_rejected_with_413(self, client):
        # Body well over MAX_PAYLOAD → Content-Length fast-reject.
        response = client.post("/json", json={"blob": "x" * (MAX_PAYLOAD * 3)})
        assert response.status_code == 413


class TestMultipartPayloadLimit:
    def test_small_upload_succeeds(self, client):
        response = client.post(
            "/upload",
            files={"avatar": ("small.bin", b"x" * 100, "application/octet-stream")},
        )
        assert response.status_code == 200
        assert response.json()["size"] == 100

    def test_oversized_upload_rejected_with_413(self, client):
        # Total multipart body exceeds MAX_PAYLOAD → 413 (not 422), matching the
        # non-multipart path. Previously multipart had no aggregate fast-reject.
        response = client.post(
            "/upload",
            files={"avatar": ("big.bin", b"x" * (MAX_PAYLOAD * 3), "application/octet-stream")},
        )
        assert response.status_code == 413

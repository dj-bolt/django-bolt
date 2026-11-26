"""Media files that a browser can run as script are downloads.

Bolt decides by the content type that the response would carry: HTML, an XML
MIME type, a JavaScript MIME type or WebAssembly. Static files are the files
of the site, so they keep their own type.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

SCRIPT = b'<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>'

# The ten extensions of the audit finding, then the common script types.
SCRIPT_NAMES = [
    "evil.xht",
    "evil.rss",
    "evil.atom",
    "evil.rdf",
    "evil.mathml",
    "evil.xsd",
    "evil.dtd",
    "evil.es",
    "evil.jsm",
    "evil.ecma",
    "evil.svg",
    "evil.html",
    "evil.xml",
    "evil.js",
    "evil.mjs",
    "evil.wasm",
    "EVIL.XHT",
    "x.png.xht",
]

INLINE_TYPES = [
    ("photo.png", "image/png"),
    ("paper.pdf", "application/pdf"),
    ("notes.txt", "text/plain; charset=utf-8"),
]


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    with override_settings(MEDIA_URL="/media/", MEDIA_ROOT=str(root)), TestClient(BoltAPI()) as client:
        yield root, client


def assert_download(response) -> None:
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_media_that_can_run_script_is_a_download(media, name):
    root, client = media
    (root / name).write_bytes(SCRIPT)

    response = client.get(f"/media/{name}")

    assert response.status_code == 200
    assert_download(response)
    assert response.content == SCRIPT


@pytest.mark.parametrize(("name", "content_type"), INLINE_TYPES)
def test_media_of_a_safe_type_keeps_its_type(media, name, content_type):
    root, client = media
    (root / name).write_bytes(SCRIPT)

    response = client.get(f"/media/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == SCRIPT


@pytest.mark.parametrize("name", ["upload.unknownext", "upload"])
def test_media_of_an_unknown_type_is_an_octet_stream_download(media, name):
    root, client = media
    (root / name).write_bytes(SCRIPT)

    response = client.get(f"/media/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_range_of_a_script_file_is_a_download(media):
    root, client = media
    (root / "evil.xht").write_bytes(SCRIPT)

    response = client.get("/media/evil.xht", headers={"Range": "bytes=0-5"})

    assert response.status_code == 206
    assert_download(response)
    assert response.content == SCRIPT[:6]


def test_head_of_a_script_file_is_a_download(media):
    root, client = media
    (root / "evil.xht").write_bytes(SCRIPT)

    response = client.head("/media/evil.xht")

    assert response.status_code == 200
    assert_download(response)


def test_not_modified_for_a_script_file_is_a_download(media):
    root, client = media
    (root / "evil.xht").write_bytes(SCRIPT)
    etag = client.get("/media/evil.xht").headers["etag"]

    response = client.get("/media/evil.xht", headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert_download(response)


def test_static_files_keep_their_script_types(tmp_path):
    """Static files are the files of the site, so a page or a script shows inline."""
    root = tmp_path / "static"
    root.mkdir()
    for name in ("page.xht", "app.js", "logo.svg"):
        (root / name).write_bytes(SCRIPT)
    settings = {"STATIC_URL": "/static/", "STATICFILES_DIRS": [str(root)]}
    with override_settings(**settings), TestClient(BoltAPI()) as client:
        types = {name: client.get(f"/static/{name}").headers for name in ("page.xht", "app.js", "logo.svg")}

    assert types["page.xht"]["content-type"] == "application/xhtml+xml"
    assert types["app.js"]["content-type"] == "text/javascript"
    assert types["logo.svg"]["content-type"] == "image/svg+xml"
    for headers in types.values():
        assert headers.get("content-disposition", "inline").startswith("inline")

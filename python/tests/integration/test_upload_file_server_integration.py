"""Subprocess-based file-upload tests.

These exercise real multipart streaming over TCP through the actix-web pipeline
(which TestClient cannot fully reproduce), catching regressions in body reading,
Rust multipart parsing, and payload-size enforcement.

The app under test lives in ``apps/upload.py`` — a real, lint-checked module
installed verbatim into the subprocess.
"""

from __future__ import annotations

import hashlib

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_single_file_upload_roundtrips_bytes(make_server_project):
    project = make_server_project(api_module=app_module("upload"))
    payload = b"\x89PNG\r\n\x1a\n" + b"pixels" * 2048

    with project.start() as server:
        response = server.request(
            "POST",
            "/upload/single",
            files={"file": ("photo.png", payload, "image/png")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "photo.png"
    assert body["content_type"] == "image/png"
    assert body["size"] == len(payload)
    assert body["sha256"] == _sha256(payload)


def test_multiple_files_upload_preserves_each_payload(make_server_project):
    project = make_server_project(api_module=app_module("upload"))
    files_payload = [
        ("a.bin", b"first-file-contents" * 64),
        ("b.bin", b"second-file" * 128),
        ("c.bin", b"third"),
    ]

    with project.start() as server:
        response = server.request(
            "POST",
            "/upload/multiple",
            files=[("files", (name, data, "application/octet-stream")) for name, data in files_payload],
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 3
    by_name = {entry["filename"]: entry for entry in body["files"]}
    for name, data in files_payload:
        assert by_name[name]["size"] == len(data)
        assert by_name[name]["sha256"] == _sha256(data)


def test_struct_form_with_file_and_text_fields(make_server_project):
    project = make_server_project(api_module=app_module("upload"))
    payload = b"avatar-bytes" * 512

    with project.start() as server:
        response = server.request(
            "POST",
            "/upload/profile",
            data={"name": "Jane"},
            files={"avatar": ("me.png", payload, "image/png")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Jane"
    assert body["avatar_filename"] == "me.png"
    assert body["avatar_size"] == len(payload)
    assert body["avatar_sha256"] == _sha256(payload)


def test_upload_exceeding_max_size_is_rejected(make_server_project):
    project = make_server_project(api_module=app_module("upload"))
    oversized = b"x" * 4096  # endpoint caps at 1024 bytes

    with project.start() as server:
        response = server.request(
            "POST",
            "/upload/size-capped",
            files={"file": ("big.bin", oversized, "application/octet-stream")},
        )

    assert response.status_code == 422, response.text

"""Subprocess-based file-upload tests.

These exercise real multipart streaming over TCP through the actix-web pipeline
(which TestClient cannot fully reproduce), catching regressions in body reading,
Rust multipart parsing, and payload-size enforcement.
"""

from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.server_integration


UPLOAD_API_BODY = """
import hashlib

from typing import Annotated
import msgspec

from django_bolt import UploadFile
from django_bolt.params import File, Form


class ProfileForm(msgspec.Struct):
    name: str
    avatar: Annotated[UploadFile, File(max_size=5_000_000, allowed_types=["image/*"])]


@api.post("/upload/single")
async def upload_single(file: Annotated[UploadFile, File(max_size=5_000_000)]):
    content = await file.read()
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


@api.post("/upload/multiple")
async def upload_multiple(files: Annotated[list[UploadFile], File(max_files=5)]):
    digests = []
    for upload in files:
        data = await upload.read()
        digests.append({
            "filename": upload.filename,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return {"count": len(digests), "files": digests}


@api.post("/upload/profile")
async def upload_profile(data: Annotated[ProfileForm, Form()]):
    content = await data.avatar.read()
    return {
        "name": data.name,
        "avatar_filename": data.avatar.filename,
        "avatar_size": len(content),
        "avatar_sha256": hashlib.sha256(content).hexdigest(),
    }


@api.post("/upload/size-capped")
async def upload_size_capped(file: Annotated[UploadFile, File(max_size=1024)]):
    content = await file.read()
    return {"size": len(content)}
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_single_file_upload_roundtrips_bytes(make_server_project):
    project = make_server_project(project_api_body=UPLOAD_API_BODY)
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
    project = make_server_project(project_api_body=UPLOAD_API_BODY)
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
    project = make_server_project(project_api_body=UPLOAD_API_BODY)
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
    project = make_server_project(project_api_body=UPLOAD_API_BODY)
    oversized = b"x" * 4096  # endpoint caps at 1024 bytes

    with project.start() as server:
        response = server.request(
            "POST",
            "/upload/size-capped",
            files={"file": ("big.bin", oversized, "application/octet-stream")},
        )

    assert response.status_code == 422, response.text

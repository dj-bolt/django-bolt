"""App under test for real multipart file uploads over TCP.

Covers single/multiple uploads, a struct form mixing text + file fields, and
payload-size enforcement.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

import msgspec

from django_bolt import BoltAPI, UploadFile
from django_bolt.params import File, Form

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


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
        digests.append(
            {
                "filename": upload.filename,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
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

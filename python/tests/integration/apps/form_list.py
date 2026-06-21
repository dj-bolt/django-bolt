"""App under test for form ``list[T]`` field integration.

Exercises the FormValue Single/Multi grouping and Python-side scalar-to-list
wrapping for both urlencoded and multipart bodies.
"""

from __future__ import annotations

from typing import Annotated

import msgspec

from django_bolt import BoltAPI
from django_bolt.params import Form

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


class TagsForm(msgspec.Struct):
    name: str
    tags: list[str] = []
    counts: list[int] = []


@api.post("/form-list")
async def handle_form_list(data: Annotated[TagsForm, Form()]):
    return {
        "name": data.name,
        "tags": data.tags,
        "counts": data.counts,
    }

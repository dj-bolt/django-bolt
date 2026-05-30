"""Subprocess-based form `list[T]` field tests.

The urlencoded and multipart codecs both allow a key to appear multiple times.
TestClient already covers the unit-level wiring; these tests exercise the real
TCP + actix-web + Rust form parser to catch regressions in the FormValue
Single/Multi grouping and the Python-side scalar-to-list wrapping.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.server_integration


FORM_LIST_API_BODY = """
from typing import Annotated

import msgspec

from django_bolt.params import Form


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
"""


def test_urlencoded_repeated_keys_bind_to_list(make_server_project):
    project = make_server_project(project_api_body=FORM_LIST_API_BODY)
    body = "name=alice&tags=red&tags=green&tags=blue&counts=1&counts=2"

    with project.start() as server:
        response = server.request(
            "POST",
            "/form-list",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "alice"
    assert data["tags"] == ["red", "green", "blue"]
    assert data["counts"] == [1, 2]


def test_multipart_repeated_keys_bind_to_list(make_server_project):
    """
    Verify that multipart/form-data repeated keys are bound to list fields and scalar values are converted and wrapped as needed.
    
    Sends a multipart POST to /form-list with multiple `tags`, a single `name`, and a single numeric `counts` value, then asserts the response status is 200 and the JSON body contains:
    - name: "bob"
    - tags: ["x", "y", "z"]
    - counts: [10]
    """
    project = make_server_project(project_api_body=FORM_LIST_API_BODY)

    with project.start() as server:
        response = server.request(
            "POST",
            "/form-list",
            files=[
                ("name", (None, "bob")),
                ("tags", (None, "x")),
                ("tags", (None, "y")),
                ("tags", (None, "z")),
                ("counts", (None, "10")),
            ],
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "bob"
    assert data["tags"] == ["x", "y", "z"]
    assert data["counts"] == [10]


def test_single_urlencoded_value_wraps_to_one_element_list(make_server_project):
    project = make_server_project(project_api_body=FORM_LIST_API_BODY)

    with project.start() as server:
        response = server.request(
            "POST",
            "/form-list",
            data={"name": "carol", "tags": "solo"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "carol"
    assert data["tags"] == ["solo"]
    assert data["counts"] == []


def test_missing_list_field_uses_struct_default(make_server_project):
    project = make_server_project(project_api_body=FORM_LIST_API_BODY)

    with project.start() as server:
        response = server.request(
            "POST",
            "/form-list",
            data={"name": "dave"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "dave"
    assert data["tags"] == []
    assert data["counts"] == []

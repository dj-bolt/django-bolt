"""Subprocess-based form `list[T]` field tests.

The urlencoded and multipart codecs both allow a key to appear multiple times.
TestClient already covers the unit-level wiring; these tests exercise the real
TCP + actix-web + Rust form parser to catch regressions in the FormValue
Single/Multi grouping and the Python-side scalar-to-list wrapping.

The app under test lives in ``apps/form_list.py`` — a real, lint-checked module.
The subprocess imports it by path via ``BOLT_API`` (``api_module``) and the
in-process ``TestClient`` below imports the same module object: one source,
tested at two altitudes.
"""

from __future__ import annotations

import pytest

from django_bolt.testing import TestClient

from .apps import app_module, form_list


@pytest.mark.server_integration
def test_urlencoded_repeated_keys_bind_to_list(make_server_project):
    project = make_server_project(api_module=app_module("form_list"))
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


@pytest.mark.server_integration
def test_multipart_repeated_keys_bind_to_list(make_server_project):
    """
    Verify that multipart/form-data repeated keys are bound to list fields and scalar values are converted and wrapped as needed.

    Sends a multipart POST to /form-list with multiple `tags`, a single `name`, and a single numeric `counts` value, then asserts the response status is 200 and the JSON body contains:
    - name: "bob"
    - tags: ["x", "y", "z"]
    - counts: [10]
    """
    project = make_server_project(api_module=app_module("form_list"))

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


@pytest.mark.server_integration
def test_single_urlencoded_value_wraps_to_one_element_list(make_server_project):
    project = make_server_project(api_module=app_module("form_list"))

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


@pytest.mark.server_integration
def test_missing_list_field_uses_struct_default(make_server_project):
    project = make_server_project(api_module=app_module("form_list"))

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


def test_repeated_keys_bind_to_list_in_process():
    """Same app module, exercised in-process — fast feedback on the binding logic
    without spawning a server. The subprocess tests above cover the real TCP path."""
    with TestClient(form_list.api) as client:
        response = client.post(
            "/form-list",
            data={"name": "erin", "tags": ["red", "green"], "counts": ["1", "2", "3"]},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "erin"
    assert data["tags"] == ["red", "green"]
    assert data["counts"] == [1, 2, 3]

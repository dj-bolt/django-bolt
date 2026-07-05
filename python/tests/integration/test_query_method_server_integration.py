"""Real-server tests for the HTTP QUERY method.

These run against an actual ``runbolt`` process over TCP, which is the only way
to prove actix-web accepts and routes the non-standard ``QUERY`` method off the
wire. The in-process ``TestClient`` builds the method a different way
(``Method::from_bytes`` in ``src/testing.rs``), so it can't stand in for the
real socket path here. In-process behavior (validation, coexistence with GET,
OpenAPI, CBV) is covered in ``python/tests/test_query_method.py``.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def test_query_with_body_over_real_tcp(make_server_project):
    project = make_server_project(api_module=app_module("query"))

    with project.start() as server:
        response = server.request(
            "QUERY",
            "/search",
            json={"term": "django", "limit": 7},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"term": "django", "limit": 7}


def test_query_body_is_validated_over_real_tcp(make_server_project):
    """A bad QUERY body must be read and rejected by the real server, not routed
    blindly. Missing the required ``term`` field returns 422."""
    project = make_server_project(api_module=app_module("query"))

    with project.start() as server:
        response = server.request("QUERY", "/search", json={"limit": 7})

    assert response.status_code == 422, response.text

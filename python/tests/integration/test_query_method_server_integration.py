"""Real-server tests for the HTTP QUERY method."""

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

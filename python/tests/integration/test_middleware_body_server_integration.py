"""A route behind middleware gets the body on a real server.

The server does not read the body of a route whose handler does not read it.
Before the fix, a route behind middleware also got no body. Thus
``CsrfViewMiddleware`` found no form token, and a valid form POST got 403.

This test needs runbolt. The request pipeline of TestClient reads the body of
each request, so TestClient does not show the bug.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

SUBMIT_PATHS = ("/submit", "/submit-sync")


def test_a_csrf_form_post_passes_when_the_handler_does_not_read_the_body(make_server_project):
    project = make_server_project(api_module=app_module("middleware_body"))

    with project.start() as server:
        token = server.get("/token").json()["token"]
        assert server.client.cookies.get("csrftoken")
        responses = {path: server.request("POST", path, data={"csrfmiddlewaretoken": token}) for path in SUBMIT_PATHS}

    assert {path: response.status_code for path, response in responses.items()} == dict.fromkeys(SUBMIT_PATHS, 200)
    assert {path: response.json() for path, response in responses.items()} == dict.fromkeys(
        SUBMIT_PATHS, {"status": "submitted"}
    )


def test_a_python_middleware_reads_the_body_when_the_handler_does_not(make_server_project):
    project = make_server_project(api_module=app_module("middleware_body"))

    with project.start() as server:
        response = server.request("POST", "/python/echo", content=b"payload")

    assert response.status_code == 200, response.text
    assert response.headers["x-body"] == "payload"

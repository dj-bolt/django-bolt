"""``runbolt`` runs the middleware of the API for a request that Rust answers (issue #865)."""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import access_log_settings


@pytest.mark.server_integration
def test_the_middleware_and_the_access_log_get_each_answer_of_rust(make_server_project):
    project = make_server_project(
        api_module=app_module("rust_answer_middleware"),
        settings_extra=access_log_settings(),
    )
    with project.start(startup_path="/health") as server:
        responses = [
            server.request("GET", "/open"),
            server.request("GET", "/sync"),
            server.request("GET", "/no-route"),
            # skip_paths of the API applies to a path with no route too.
            server.request("GET", "/skipped"),
            server.request("POST", "/open"),
            server.request("GET", "/private"),
        ]
        # The bucket has one token. Send requests until one gets a 429.
        limited = [server.request("GET", "/limited")]
        while limited[-1].status_code != 429 and len(limited) < 20:
            limited.append(server.request("GET", "/limited"))
    assert [response.status_code for response in responses] == [200, 200, 404, 404, 405, 401]
    # The middleware ran on one thread for each request, also for an answer of Rust.
    assert [response.headers.get("x-tenant") for response in responses] == ["acme"] * 6
    assert responses[2].json() == {"detail": "Not Found"}
    # A 429 stays in Rust: the middleware does not get it.
    assert [response.status_code for response in limited[-2:]] == [200, 429]
    assert [response.headers.get("x-tenant") for response in limited] == ["acme"] * (len(limited) - 1) + [None]

    lines = [line.rsplit(" ", 1)[0] for line in project.path("access.log").read_text().splitlines()]
    assert lines == [
        "INFO GET /open 200",
        "INFO GET /sync 200",
        "WARNING GET /no-route 404",
        "WARNING POST /open 405",
        "WARNING GET /private 401",
        *["INFO GET /limited 200"] * (len(limited) - 1),
        "WARNING GET /limited 429",
    ]

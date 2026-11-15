"""The Rust access log has a line for a response that Rust gives before the handler."""

from __future__ import annotations

import pytest

from .apps import app_module

_LOGGING = """
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(message)s"}},
    "handlers": {
        "file": {
            "class": "logging.FileHandler",
            "filename": str(BASE_DIR / "access.log"),
            "formatter": "plain",
        }
    },
    "loggers": {
        "django.server": {"handlers": ["file"], "level": "INFO", "propagate": False}
    },
}
"""


@pytest.mark.server_integration
def test_access_log_has_a_line_for_each_response_of_a_route(make_server_project):
    project = make_server_project(
        api_module=app_module("access_log_rejections"),
        settings_extra=_LOGGING,
    )
    with project.start(startup_path="/health") as server:
        assert server.request("GET", "/open").status_code == 200
        assert server.request("GET", "/private").status_code == 401
        assert server.request("GET", "/private", headers={"x-api-key": "secret"}).status_code == 200
        assert server.request("GET", "/limited").status_code == 200
        assert server.request("GET", "/limited").status_code == 429
        assert server.request("GET", "/items/abc").status_code == 422
        assert server.request("GET", "/open/", follow_redirects=False).status_code == 308
        assert server.request("OPTIONS", "/open").status_code == 204
        # The path has a route, but not for this method.
        assert server.request("POST", "/open").status_code == 405
        assert server.request("GET", "/no-route").status_code == 404
    lines = [line.rsplit(" ", 1)[0] for line in project.path("access.log").read_text().splitlines()]

    assert [line for line in lines if "/health" not in line] == [
        "INFO GET /open 200",
        "WARNING GET /private 401",
        "INFO GET /private 200",
        "INFO GET /limited 200",
        "WARNING GET /limited 429",
        "WARNING GET /items/abc 422",
        "INFO GET /open/ 308",
        "INFO OPTIONS /open 204",
        "WARNING POST /open 405",
    ]

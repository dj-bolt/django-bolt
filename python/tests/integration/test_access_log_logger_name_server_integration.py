"""The Rust access log writes to the logger that ``LoggingConfig.logger_name`` names."""

from __future__ import annotations

import pytest

from .apps import app_module

# myapp.api keeps INFO and above. The test sets the level of django.server.
_LOGGING = """
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(name)s %(levelname)s %(message)s"}},
    "handlers": {
        "server_file": {
            "class": "logging.FileHandler",
            "filename": str(BASE_DIR / "server.log"),
            "formatter": "plain",
        },
        "named_file": {
            "class": "logging.FileHandler",
            "filename": str(BASE_DIR / "named.log"),
            "formatter": "plain",
        },
    },
    "loggers": {
        "django.server": {"handlers": ["server_file"], "level": "{server_level}", "propagate": False},
        "myapp.api": {"handlers": ["named_file"], "level": "INFO", "propagate": False},
    },
}
"""


def _lines(project, name):
    return [line.rsplit(" ", 1)[0] for line in project.path(name).read_text().splitlines()]


def _run(make_server_project, server_level):
    """Send the requests and return the lines of the two logs."""
    project = make_server_project(
        api_module=app_module("access_log_logger_name"),
        settings_extra=_LOGGING.replace("{server_level}", server_level),
    )
    with project.start(startup_path="/health") as server:
        assert server.request("GET", "/open").status_code == 200
        assert server.request("GET", "/broken").status_code == 404
        # A path with no route writes its 404 line to the logger of the first API.
        assert server.request("GET", "/nowhere").status_code == 404
        assert server.request("GET", "/named/ping").status_code == 200
        assert server.request("GET", "/named/ping/", follow_redirects=False).status_code == 308
        assert server.request("OPTIONS", "/named/ping").status_code == 204
        assert server.request("GET", "/named/gone").status_code == 404
    return _lines(project, "server.log"), _lines(project, "named.log")


# The redirect and the automatic OPTIONS answer use the logger of the route.
_NAMED_LINES = [
    "myapp.api INFO GET /named/ping 200",
    "myapp.api INFO GET /named/ping/ 308",
    "myapp.api INFO OPTIONS /named/ping 204",
    "myapp.api WARNING GET /named/gone 404",
]


@pytest.mark.server_integration
def test_access_log_writes_to_the_logger_of_the_logging_config(make_server_project):
    server_lines, named_lines = _run(make_server_project, "WARNING")
    # Each line goes to the logger of the API of its route, at the level of that logger.
    assert server_lines == ["django.server WARNING GET /broken 404", "django.server WARNING GET /nowhere 404"]
    assert named_lines == _NAMED_LINES


@pytest.mark.server_integration
def test_access_log_starts_when_only_a_named_logger_writes(make_server_project):
    """django.server writes no access-log level, but myapp.api writes INFO."""
    server_lines, named_lines = _run(make_server_project, "CRITICAL")
    assert server_lines == []
    assert named_lines == _NAMED_LINES


@pytest.mark.server_integration
def test_a_path_with_no_route_writes_its_404_when_no_route_logger_writes(make_server_project):
    """The first API has no route. Its 404 line goes to django.server, and myapp.api writes nothing."""
    logging_settings = _LOGGING.replace("{server_level}", "INFO").replace(
        '"myapp.api": {"handlers": ["named_file"], "level": "INFO"',
        '"myapp.api": {"handlers": ["named_file"], "level": "CRITICAL"',
    )
    project = make_server_project(api_module=app_module("access_log_no_route_logger"), settings_extra=logging_settings)
    with project.start(startup_path="/named/health") as server:
        assert server.request("GET", "/nowhere").status_code == 404
    assert _lines(project, "server.log") == ["django.server WARNING GET /nowhere 404"]

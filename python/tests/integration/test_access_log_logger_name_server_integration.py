"""The Rust access log writes to the logger that ``LoggingConfig.logger_name`` names."""

from __future__ import annotations

import pytest

from .apps import app_module

# django.server keeps only WARNING and above. myapp.api keeps INFO and above.
# Thus the access log must start, also when django.server has no INFO line.
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
        "django.server": {"handlers": ["server_file"], "level": "WARNING", "propagate": False},
        "myapp.api": {"handlers": ["named_file"], "level": "INFO", "propagate": False},
    },
}
"""


def _lines(project, name):
    return [line.rsplit(" ", 1)[0] for line in project.path(name).read_text().splitlines()]


@pytest.mark.server_integration
def test_access_log_writes_to_the_logger_of_the_logging_config(make_server_project):
    project = make_server_project(
        api_module=app_module("access_log_logger_name"),
        settings_extra=_LOGGING,
    )
    with project.start(startup_path="/health") as server:
        assert server.request("GET", "/open").status_code == 200
        assert server.request("GET", "/broken").status_code == 404
        assert server.request("GET", "/named/ping").status_code == 200
        assert server.request("GET", "/named/gone").status_code == 404

    # Each line goes to the logger of the API of its route, at the level of that logger.
    assert _lines(project, "server.log") == ["django.server WARNING GET /broken 404"]
    assert _lines(project, "named.log") == [
        "myapp.api INFO GET /named/ping 200",
        "myapp.api WARNING GET /named/gone 404",
    ]

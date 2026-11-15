"""The Rust access log applies ``skip_paths`` and ``skip_status_codes`` of ``LoggingConfig``."""

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
def test_access_log_skips_the_paths_and_the_status_codes_of_a_logging_config(make_server_project):
    project = make_server_project(
        api_module=app_module("access_log_skips"),
        settings_extra=_LOGGING,
    )
    with project.start(startup_path="/health") as server:
        # The default config skips /health and /ready, also for an error.
        assert server.request("GET", "/health").status_code == 200
        assert server.request("GET", "/ready").status_code == 503
        assert server.request("GET", "/open").status_code == 200
        # skip_paths holds complete request paths.
        assert server.request("GET", "/quiet/ping").status_code == 200
        assert server.request("GET", "/quiet/health").status_code == 200
        assert server.request("GET", "/quiet/items/7").status_code == 200
        assert server.request("GET", "/quiet/items/8").status_code == 200
        # skip_status_codes applies to each status class.
        assert server.request("DELETE", "/quiet/items/8").status_code == 204
        assert server.request("GET", "/quiet/gone").status_code == 404
        assert server.request("GET", "/quiet/down").status_code == 503
    lines = [line.rsplit(" ", 1)[0] for line in project.path("access.log").read_text().splitlines()]

    assert lines == [
        "INFO GET /open 200",
        "INFO GET /quiet/health 200",
        "INFO GET /quiet/items/8 200",
        "ERROR GET /quiet/down 503",
    ]

"""The Rust access log picks the level from the response status, like Django."""

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
        "django.server": {"handlers": ["file"], "level": "{level}", "propagate": False}
    },
}
"""


def _run(make_server_project, level: str) -> list[str]:
    project = make_server_project(
        api_module=app_module("access_log_levels"),
        settings_extra=_LOGGING.replace("{level}", level),
    )
    with project.start(startup_path="/health") as server:
        for path in ("/health", "/gone", "/down", "/teapot"):
            server.request("GET", path)
    return project.path("access.log").read_text().splitlines()


@pytest.mark.server_integration
def test_access_log_levels_follow_status(make_server_project):
    lines = _run(make_server_project, "INFO")
    assert any(line.startswith("INFO GET /health 200") for line in lines)
    assert any(line.startswith("WARNING GET /gone 404") for line in lines)
    assert any(line.startswith("WARNING GET /teapot 418") for line in lines)
    assert any(line.startswith("ERROR GET /down 503") for line in lines)


@pytest.mark.server_integration
def test_access_log_at_warning_keeps_only_errors(make_server_project):
    lines = _run(make_server_project, "WARNING")
    assert not any("/health" in line for line in lines)
    assert any(line.startswith("WARNING GET /gone 404") for line in lines)
    assert any(line.startswith("ERROR GET /down 503") for line in lines)

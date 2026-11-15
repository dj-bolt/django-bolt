"""The Rust access log applies the sample rate and the slow-request threshold."""

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

_PATHS = ("/fast", "/fast", "/slow", "/gone", "/sampled/ok", "/sampled/ok", "/sampled/gone", "/sampled/down")


def _run(make_server_project, settings: str) -> list[str]:
    project = make_server_project(
        api_module=app_module("access_log_filters"),
        settings_extra=_LOGGING + settings,
    )
    with project.start(startup_path="/health") as server:
        for path in _PATHS:
            server.request("GET", path)
    lines = project.path("access.log").read_text().splitlines()
    # Drop the duration, and the lines of the readiness probe.
    return [line.rsplit(" ", 1)[0] for line in lines if "/health" not in line]


@pytest.mark.server_integration
def test_access_log_applies_the_sample_rate_of_a_logging_config(make_server_project):
    lines = _run(make_server_project, "")
    # The API with sample_rate=0.0 writes only its errors. The other API has no limit.
    assert lines == [
        "INFO GET /fast 200",
        "INFO GET /fast 200",
        "INFO GET /slow 200",
        "WARNING GET /gone 404",
        "WARNING GET /sampled/gone 404",
        "ERROR GET /sampled/down 503",
    ]


@pytest.mark.server_integration
def test_access_log_filters_the_redirect_and_the_options_answer_of_a_route(make_server_project):
    project = make_server_project(
        api_module=app_module("access_log_filters"),
        settings_extra=_LOGGING,
    )
    with project.start(startup_path="/health") as server:
        # Rust gives these answers before the handler runs.
        assert server.request("GET", "/sampled/ok/", follow_redirects=False).status_code == 308
        assert server.request("OPTIONS", "/sampled/ok").status_code == 204
        assert server.request("GET", "/fast/", follow_redirects=False).status_code == 308
        assert server.request("OPTIONS", "/fast").status_code == 204
        assert server.request("GET", "/sampled/gone").status_code == 404
    lines = project.path("access.log").read_text().splitlines()
    # The API with sample_rate=0.0 writes no line for its 308 and its 204.
    assert [line.rsplit(" ", 1)[0] for line in lines if "/health" not in line] == [
        "INFO GET /fast/ 308",
        "INFO OPTIONS /fast 204",
        "WARNING GET /sampled/gone 404",
    ]


@pytest.mark.server_integration
def test_access_log_applies_the_slow_request_setting(make_server_project):
    lines = _run(make_server_project, "DJANGO_BOLT_LOG_SLOW_MS = 200\n")
    assert lines == [
        "INFO GET /slow 200",
        "WARNING GET /gone 404",
        "WARNING GET /sampled/gone 404",
        "ERROR GET /sampled/down 503",
    ]


@pytest.mark.server_integration
def test_access_log_applies_the_sample_setting(make_server_project):
    lines = _run(make_server_project, "DJANGO_BOLT_LOG_SAMPLE = 0.0\n")
    assert lines == [
        "WARNING GET /gone 404",
        "WARNING GET /sampled/gone 404",
        "ERROR GET /sampled/down 503",
    ]

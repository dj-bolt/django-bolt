"""Log records of the Rust core reach stderr, filtered by the level of the django_bolt logger.

Rust reads the level once, when the server or a test app starts. A record of a
level below it costs one atomic load in Rust and is not formatted.
"""

from __future__ import annotations

import logging

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

ASGI_ERROR = "[django-bolt] ERROR bolt_asgi::asgi_http: ASGI mount: coroutine raised an exception: RuntimeError: boom"


def _failing_mount_api() -> BoltAPI:
    api = BoltAPI()

    async def failing_app(scope, receive, send):
        raise RuntimeError("boom")

    api.mount_asgi("/app", failing_app)
    return api


@pytest.fixture
def bolt_logger_level():
    """Set the level of the django_bolt logger, and restore it after the test."""
    bolt_logger = logging.getLogger("django_bolt")
    previous = bolt_logger.level

    yield bolt_logger.setLevel

    bolt_logger.setLevel(previous)


def test_a_rust_error_record_reaches_stderr(capfd, bolt_logger_level):
    bolt_logger_level(logging.WARNING)

    with TestClient(_failing_mount_api(), raise_server_exceptions=False) as client:
        assert client.get("/app/x").status_code == 500

    assert ASGI_ERROR in capfd.readouterr().err.splitlines()


def test_the_django_bolt_logger_level_filters_rust_records(capfd, bolt_logger_level):
    bolt_logger_level(logging.CRITICAL)

    with TestClient(_failing_mount_api(), raise_server_exceptions=False) as client:
        assert client.get("/app/x").status_code == 500

    assert "bolt_asgi::asgi_http" not in capfd.readouterr().err

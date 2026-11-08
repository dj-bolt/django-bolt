"""The Origin check of a WebSocket handshake on a real runbolt server.

The test client does its own handshake. Only these tests reach the check of
the server.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import attempt_ws_upgrade

pytestmark = pytest.mark.server_integration

REJECTED_BODY = '{"detail":"Origin not allowed"}'


def _handshake(server, origin: str | None) -> tuple[str, str]:
    headers = {} if origin is None else {"Origin": origin}
    return attempt_ws_upgrade(server.host, server.port, "/ws/echo", headers=headers)


def test_configured_origins_pass_and_other_origins_get_403(make_server_project):
    project = make_server_project(
        settings_extra="""
        CORS_ALLOWED_ORIGINS = ["https://allowed.example"]
        CORS_ALLOWED_ORIGIN_REGEXES = [r"^https://[a-z]+\\.trusted\\.example$"]
        """,
        api_module=app_module("websocket_origin"),
    )

    with project.start() as server:
        exact = _handshake(server, "https://allowed.example")
        regex = _handshake(server, "https://app.trusted.example")
        no_origin = _handshake(server, None)
        other = _handshake(server, "https://evil.example")
        near_miss = _handshake(server, "https://allowed.example.evil.example")

    assert " 101 " in exact[0]
    assert " 101 " in regex[0]
    # A browser sends no Origin header for a same-origin connection.
    assert " 101 " in no_origin[0]
    for status_line, body in (other, near_miss):
        assert " 403 " in status_line
        assert body == REJECTED_BODY


def test_no_cors_settings_reject_every_cross_origin_handshake(make_server_project):
    project = make_server_project(api_module=app_module("websocket_origin"))

    with project.start() as server:
        cross_origin = _handshake(server, "https://any.example")
        no_origin = _handshake(server, None)

    assert " 403 " in cross_origin[0]
    assert cross_origin[1] == REJECTED_BODY
    assert " 101 " in no_origin[0]

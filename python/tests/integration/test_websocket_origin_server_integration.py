"""The Origin check of a WebSocket handshake on a real runbolt server.

The test client does its own handshake. Only these tests reach the check of
the server.
"""

from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

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
    # Non-browser clients may omit Origin; this does not establish same-origin.
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


def test_native_custom_and_any_on_real_server(make_server_project):
    project = make_server_project(
        api_module=app_module("websocket_origin"),
        settings_extra='BOLT_WEBSOCKET_ORIGIN_POLICY = "allowlist"\n'
        'BOLT_WEBSOCKET_ALLOWED_ORIGINS = ["https://global.example"]',
    )
    with project.start() as server:
        def check(path, origin):
            headers = {} if origin is None else {"Origin": origin}
            return attempt_ws_upgrade(server.host, server.port, path, headers=headers)

        trusted, _ = check("/ws/tenant", "https://tenant.example")
        evil, evil_body = check("/ws/tenant", "https://evil.example")
        missing, missing_body = check("/ws/tenant", None)
        public, _ = check("/ws/public", "https://random.example")
        public_missing, _ = check("/ws/public", None)
        default_reject, _ = check("/ws/echo", "https://random.example")
        default_allowed, _ = check("/ws/echo", "https://global.example")

    assert " 101 " in trusted
    assert " 403 " in evil and evil_body == REJECTED_BODY
    assert " 403 " in missing and missing_body == REJECTED_BODY
    assert " 101 " in public
    assert " 101 " in public_missing
    assert " 403 " in default_reject
    assert " 101 " in default_allowed


@pytest.mark.parametrize("origin", [
    "https://example.com:0", "https://example.com:65536", "https://example.com:",
    "https://[not-ipv6]", "https://user@example.com", "https://example.com/path",
])
def test_any_rejects_malformed_origins_before_upgrade(make_server_project, origin):
    project = make_server_project(api_module=app_module("websocket_origin"))
    with project.start() as server:
        status, body = attempt_ws_upgrade(
            server.host, server.port, "/ws/public", headers={"Origin": origin}
        )
    assert " 403 " in status
    assert body == REJECTED_BODY


@pytest.mark.parametrize(("module", "path", "origin", "global_policy", "status_code"), [
    ("websocket_origin", "/ws/echo", "https://allowed.example", "inherit", 403),
    ("websocket_origin", "/ws/echo", "https://allowed.example", "allowlist", 403),
    ("websocket_origin", "/ws/public", "https://allowed.example", "inherit", 403),
    ("websocket_origin", "/ws/tenant", "https://tenant.example", "inherit", 403),
    ("websocket_origin", "/custom/ws?approve", "https://allowed.example", "inherit", 403),
    ("ws_asgi_mount", "/mounted/ws", "https://allowed.example", "inherit", 403),
    ("ws_asgi_mount", "/restricted/ws", "https://restricted.example", "inherit", 403),
    ("ws_asgi_mount", "/delegated/ws", "https://delegated.example", "inherit", 101),
])
def test_repeated_origins_follow_policy_on_real_server(make_server_project, module, path, origin, global_policy, status_code):
    project = make_server_project(
        api_module=app_module(module),
        settings_extra=f'CORS_ALLOWED_ORIGINS = ["https://allowed.example"]\n'
        f'BOLT_WEBSOCKET_ORIGIN_POLICY = "{global_policy}"\n'
        'BOLT_WEBSOCKET_ALLOWED_ORIGINS = ["https://allowed.example"]',
    )
    with project.start() as server:
        status, body = attempt_ws_upgrade(server.host, server.port, path, headers={
            "Origin": origin, "origin": origin,
        })
    assert f" {status_code} " in status
    if status_code == 403:
        assert body == REJECTED_BODY


def test_delegated_mount_preserves_repeated_origins_on_real_server(make_server_project):
    project = make_server_project(api_module=app_module("ws_asgi_mount"))
    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host, server.port, "/delegated/ws?report_origins",
            headers={"Origin": "https://delegated.example", "origin": "https://delegated.example"},
        ) as websocket,
    ):
        assert json.loads(websocket.receive_text()) == ["https://delegated.example"] * 2


@pytest.mark.parametrize("path", ["/ws/failing", "/custom/ws"])
def test_custom_failures_and_timeouts_deny_real_handshakes(make_server_project, path):
    project = make_server_project(api_module=app_module("websocket_origin"))
    with project.start() as server:
        accepted, _ = attempt_ws_upgrade(
            server.host, server.port, f"{path}?approve", headers={"Origin": "https://allowed.example"}
        )
        assert " 101 " in accepted
        for mode in ("exception", "nonbool", "timeout"):
            status, body = attempt_ws_upgrade(
                server.host, server.port, f"{path}?{mode}",
                headers={"Origin": "https://allowed.example"}, timeout=8.0,
            )
            assert " 403 " in status
            assert body == REJECTED_BODY
        # Recovery must work on this same server after a Python timeout.
        accepted, _ = attempt_ws_upgrade(
            server.host, server.port, f"{path}?approve", headers={"Origin": "https://allowed.example"}
        )
        assert " 101 " in accepted

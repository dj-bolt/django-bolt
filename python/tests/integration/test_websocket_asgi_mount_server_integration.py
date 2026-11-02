from __future__ import annotations

import json

import pytest

from .apps import app_module
from .helpers import ServerProject, SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


def _make_project(make_server_project) -> ServerProject:
    return make_server_project(api_module=app_module("ws_asgi_mount"))


def test_mounted_asgi_app_serves_websocket(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/mounted/ws") as websocket,
    ):
        websocket.receive_text()
        websocket.send_text("hello")
        assert websocket.receive_text() == "echo:hello"
        websocket.send_text("bye")
        code, reason = websocket.receive_close()

    assert code == 1000
    assert reason == "done"


@pytest.mark.parametrize(
    "cors_settings",
    ["", 'CORS_ALLOWED_ORIGINS = ["https://allowed-by-bolt.example"]'],
)
def test_default_mount_inherits_origin_policy(make_server_project, cors_settings):
    """Default mounts stay protected; no silent policy downgrade."""
    project = make_server_project(
        api_module=app_module("ws_asgi_mount"),
        settings_extra=cors_settings,
    )
    with project.start() as server:
        headers = {"Origin": "https://untrusted.example"}
        mount_status, mount_body = attempt_ws_upgrade(server.host, server.port, "/mounted/ws", headers=headers)
        native_status, native_body = attempt_ws_upgrade(server.host, server.port, "/ws/direct", headers=headers)
    assert " 403 " in mount_status, mount_status
    assert mount_body == '{"detail":"Origin not allowed"}'
    assert " 403 " in native_status, native_status
    assert native_body == '{"detail":"Origin not allowed"}'


def test_default_mount_allows_globally_configured_origin(make_server_project):
    project = make_server_project(
        api_module=app_module("ws_asgi_mount"),
        settings_extra='CORS_ALLOWED_ORIGINS = ["https://allowed-by-bolt.example"]',
    )
    with project.start() as server:
        status, _ = attempt_ws_upgrade(
            server.host, server.port, "/mounted/ws",
            headers={"Origin": "https://allowed-by-bolt.example"},
        )
    assert " 101 " in status, status


def test_strict_mount_allowlist_overrides_global_allow_all(make_server_project):
    """Dedicated ASGI security must not inherit a globally open CORS policy."""
    project = make_server_project(
        api_module=app_module("ws_asgi_mount"),
        settings_extra="CORS_ALLOW_ALL_ORIGINS = True",
    )
    with project.start() as server:
        headers = {"Origin": "https://other.example"}
        status, body = attempt_ws_upgrade(server.host, server.port, "/restricted/ws", headers=headers)
        default_status, _ = attempt_ws_upgrade(server.host, server.port, "/mounted/ws", headers=headers)

    assert " 403 " in status, status
    assert body == '{"detail":"Origin not allowed"}'
    assert " 101 " in default_status, default_status


def test_asgi_mount_explicit_allowlist_and_delegate(make_server_project):
    project = _make_project(make_server_project)
    with project.start() as server:
        def upgrade(path, origin):
            headers = {} if origin is None else {"Origin": origin}
            return attempt_ws_upgrade(server.host, server.port, path, headers=headers)

        allowlisted, _ = upgrade("/restricted/ws", "https://restricted.example")
        disallowed, disallowed_body = upgrade("/restricted/ws", "https://other.example")
        missing, missing_body = upgrade("/restricted/ws", None)
        delegated, _ = upgrade("/delegated/ws", "https://delegated.example")
        delegated_denied, _ = upgrade("/delegated/ws", "https://other.example")
        native_denied, _ = upgrade("/ws/direct", "https://delegated.example")

    assert " 101 " in allowlisted, allowlisted
    assert " 403 " in disallowed, disallowed
    assert disallowed_body == '{"detail":"Origin not allowed"}'
    assert " 403 " in missing, missing
    assert missing_body == '{"detail":"Origin not allowed"}'
    assert " 101 " in delegated, delegated
    assert " 403 " in delegated_denied, delegated_denied
    assert " 403 " in native_denied, native_denied


def test_mounted_asgi_websocket_scope_follows_spec(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host,
            server.port,
            "/mounted/ws?token=abc",
            headers={"Authorization": "Bearer secret-token"},
        ) as websocket,
    ):
        scope = json.loads(websocket.receive_text())

    # ASGI keeps the mount prefix on the path. A router strips root_path itself.
    assert scope["path"] == "/mounted/ws"
    assert scope["root_path"] == "/mounted"
    assert scope["query_string"] == "token=abc"
    assert scope["authorization"] == "Bearer secret-token"


def test_mounted_asgi_websocket_scope_decodes_path_and_keeps_utf8_headers(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host,
            server.port,
            "/mounted/caf%C3%A9/",
            headers={"X-Name": "café"},
        ) as websocket,
    ):
        scope = json.loads(websocket.receive_text())

    # ASGI decodes path. raw_path keeps the bytes from the request line.
    assert scope["path"] == "/mounted/café/"
    assert scope["raw_path"] == "/mounted/caf%C3%A9/"
    assert scope["x_name"] == "café"


@pytest.mark.parametrize(
    ("settings_extra", "scheme"),
    [
        # A client that is not a declared proxy cannot choose the scheme.
        ("", "ws"),
        # A declared proxy can. The test client connects from loopback.
        ('BOLT_TRUSTED_PROXIES = ["127.0.0.0/8"]', "wss"),
    ],
)
def test_mounted_asgi_websocket_scheme_follows_x_forwarded_proto_only_from_a_trusted_proxy(
    make_server_project, settings_extra, scheme
):
    project = make_server_project(api_module=app_module("ws_asgi_mount"), settings_extra=settings_extra)

    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host,
            server.port,
            "/mounted/ws",
            headers={"X-Forwarded-Proto": "https"},
        ) as websocket,
    ):
        assert json.loads(websocket.receive_text())["scheme"] == scheme


def test_registered_route_still_wins_over_mounts(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/direct") as websocket,
    ):
        assert websocket.receive_text() == "route"


def test_django_mount_does_not_take_websocket_connections(make_server_project):
    project = _make_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/django/ws") as websocket,
    ):
        code, reason = websocket.receive_close()

    # Django serves HTTP only, so the path stays unmatched.
    assert code == 1000
    assert reason == "Not Found"

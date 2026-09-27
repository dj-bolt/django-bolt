"""A revoked token fails the WebSocket handshake of a real ``runbolt`` server.

The production handshake (``crates/bolt-websocket``) is a separate path from
the in-process ``WebSocketTestClient`` (``src/testing.rs``), so it needs its
own test.
"""

from __future__ import annotations

import time

import jwt
import pytest

from .apps import app_module
from .apps.websocket_revocation import OTHER_SECRET, SECRET
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


def _bearer(jti: str, sid: str, secret: str = SECRET) -> dict[str, str]:
    token = jwt.encode({"sub": "1", "jti": jti, "sid": sid, "exp": int(time.time()) + 60}, secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(("path", "revoke"), [("/ws/jti", "/revoke/jti-1"), ("/ws/session", "/end-session/sid-1")])
def test_a_revoked_token_fails_the_websocket_handshake(make_server_project, path, revoke):
    project = make_server_project(api_module=app_module("websocket_revocation"))
    headers = _bearer("jti-1", "sid-1")
    with project.start() as server:
        with SimpleWebSocketClient(server.host, server.port, path, headers=headers) as websocket:
            assert websocket.receive_text() == "connected"

        assert server.request("POST", revoke).status_code == 200

        status_line, body = attempt_ws_upgrade(server.host, server.port, path, headers=headers)
        assert "401" in status_line, (status_line, body)

        # Another token still connects.
        with SimpleWebSocketClient(server.host, server.port, path, headers=_bearer("jti-2", "sid-2")) as websocket:
            assert websocket.receive_text() == "connected"


def test_each_jwt_backend_checks_its_own_handshake_tokens(make_server_project):
    """Both backends have the scheme name "jwt". Only the first backend revokes the jti."""
    project = make_server_project(api_module=app_module("websocket_revocation"))
    path = "/ws/two-backends"
    first = _bearer("shared-jti", "sid-1")
    second = _bearer("shared-jti", "sid-1", secret=OTHER_SECRET)
    with project.start() as server:
        assert server.request("POST", "/revoke/shared-jti").status_code == 200

        status_line, body = attempt_ws_upgrade(server.host, server.port, path, headers=first)
        assert "401" in status_line, (status_line, body)

        with SimpleWebSocketClient(server.host, server.port, path, headers=second) as websocket:
            assert websocket.receive_text() == "connected"

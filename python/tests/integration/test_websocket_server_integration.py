from __future__ import annotations

import pytest

from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration


def _make_websocket_project(make_server_project):
    return make_server_project(
        project_api_body="""
        from django_bolt.websocket import CloseCode


        @api.websocket("/ws/protected")
        async def protected(websocket: WebSocket):
            await websocket.accept()

            if websocket.headers.get("authorization") != "Bearer secret-token":
                await websocket.close(code=CloseCode.POLICY_VIOLATION, reason="unauthorized")
                return

            await websocket.send_text("authorized")
            async for message in websocket.iter_text():
                if message == "bye":
                    await websocket.close(code=CloseCode.NORMAL, reason="done")
                    return
                await websocket.send_text(f"echo:{message}")
        """
    )


def test_websocket_handshake_auth_and_close(make_server_project):
    project = _make_websocket_project(make_server_project)

    with (
        project.start() as server,
        SimpleWebSocketClient(
            server.host,
            server.port,
            "/ws/protected",
            headers={"Authorization": "Bearer secret-token"},
        ) as websocket,
    ):
        assert websocket.receive_text() == "authorized"
        websocket.send_text("hello")
        assert websocket.receive_text() == "echo:hello"
        websocket.send_text("bye")
        code, reason = websocket.receive_close()

    assert code == 1000
    assert reason == "done"


def test_websocket_rejects_missing_auth(make_server_project):
    project = _make_websocket_project(make_server_project)

    with project.start() as server, SimpleWebSocketClient(server.host, server.port, "/ws/protected") as websocket:
        code, reason = websocket.receive_close()

    assert code == 1008
    assert reason == "unauthorized"

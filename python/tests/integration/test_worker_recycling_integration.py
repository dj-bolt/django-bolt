"""Integration tests for worker recycling and graceful shutdown.

Covers the real ``runbolt`` server:

- SIGTERM closes active WebSocket connections with 1012 (Service Restart)
  instead of dropping them abruptly.
- ``--workers-lifetime`` recycles workers (new PID) while the server keeps
  serving throughout (spawn-first via SO_REUSEPORT).
- ``--respawn-failed-workers`` replaces a crashed worker.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time

import httpx
import pytest

from .helpers import SimpleWebSocketClient

pytestmark = pytest.mark.server_integration

RECYCLE_TIMEOUT = 30.0

PID_API_BODY = """
import os


@api.get("/pid")
async def pid():
    return {"pid": os.getpid()}


@api.get("/die")
async def die():
    os._exit(1)
"""

WS_ECHO_API_BODY = """
@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(f"echo:{message}")
"""


def _receive_close_skipping_pings(websocket: SimpleWebSocketClient) -> tuple[int, str]:
    """Receive frames until a close frame arrives (heartbeat pings may interleave)."""
    for _ in range(10):
        opcode, payload = websocket._receive_frame()
        if opcode == 0x8:
            if len(payload) >= 2:
                return int.from_bytes(payload[:2], "big"), payload[2:].decode("utf-8")
            return 1005, ""
        assert opcode in (0x9, 0xA), f"Unexpected frame opcode {opcode} while waiting for close"
    raise AssertionError("No close frame received")


def _wait_for_pid_change(server, first_pid: int, timeout: float = RECYCLE_TIMEOUT) -> int:
    payload = server.wait_for_json("/pid", lambda data: data["pid"] != first_pid, timeout=timeout)
    return payload["pid"]


def test_sigterm_closes_websockets_with_service_restart_code(make_server_project):
    project = make_server_project(project_api_body=WS_ECHO_API_BODY)

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/echo") as websocket,
    ):
        websocket.send_text("hello")
        assert websocket.receive_text() == "echo:hello"

        # Signal the server directly (single process == the server itself).
        os.kill(server.process.pid, signal.SIGTERM)

        code, reason = _receive_close_skipping_pings(websocket)

    assert code == 1012, f"Expected 1012 Service Restart close, got {code} ({reason!r})"

    # The server must also actually exit (graceful stop, not a hang).
    deadline = time.time() + 15.0
    while server.process.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    assert server.process.poll() is not None, "Server did not exit after SIGTERM"


def test_workers_lifetime_recycles_worker_without_downtime(make_server_project):
    project = make_server_project(project_api_body=PID_API_BODY)

    with project.start(
        extra_args=["--workers-lifetime", "2", "--workers-kill-timeout", "5"],
    ) as server:
        first_pid = server.get("/pid").json()["pid"]
        # The supervisor forks workers, so the worker is not the manage.py process.
        assert first_pid != server.process.pid

        new_pid = _wait_for_pid_change(server, first_pid)
        assert new_pid != first_pid

        # Server keeps serving after the recycle.
        assert server.get("/health").json() == {"status": "ok"}


def test_max_rss_recycles_worker(make_server_project):
    project = make_server_project(project_api_body=PID_API_BODY)

    # 1 MiB is far below any real worker's baseline RSS, so the very first
    # RSS check trips the limit — exercising the full CLI → supervisor →
    # /proc RSS read → graceful recycle path against a real worker.
    with project.start(
        extra_args=["--max-rss", "1", "--workers-kill-timeout", "5"],
    ) as server:
        first_pid = server.get("/pid").json()["pid"]
        new_pid = _wait_for_pid_change(server, first_pid)
        assert new_pid != first_pid


def test_respawn_failed_workers_replaces_crashed_worker(make_server_project):
    project = make_server_project(project_api_body=PID_API_BODY)

    with project.start(extra_args=["--respawn-failed-workers"]) as server:
        first_pid = server.get("/pid").json()["pid"]

        # os._exit(1) kills the worker mid-request; the connection drops.
        with contextlib.suppress(httpx.HTTPError):
            server.get("/die")

        new_pid = _wait_for_pid_change(server, first_pid)
        assert new_pid != first_pid
        assert server.get("/health").json() == {"status": "ok"}

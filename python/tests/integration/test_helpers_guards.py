from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import os
import re
import signal
import socket
import sys
import threading
import time

import pytest

from . import helpers
from .apps import app_module
from .helpers import create_server_project


def test_api_module_requires_preserved_pythonpath(tmp_path):
    with pytest.raises(ValueError, match="preserve_pythonpath"):
        create_server_project(tmp_path, api_module=app_module("hello"), preserve_pythonpath=False)


def test_dev_start_requires_on_disk_api_for_api_module(make_server_project, monkeypatch):
    class FakeServer:
        def stop(self) -> None:
            pass

    def fake_spawn_process(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(helpers, "_spawn_process", fake_spawn_process)
    monkeypatch.setattr(helpers, "RunningServer", lambda **_kwargs: FakeServer())

    project = make_server_project(api_module=app_module("hello"))
    server = None
    try:
        with pytest.raises(ValueError, match="api_source"):
            server = project.start(dev=True, port=8765)
    finally:
        if server is not None:
            server.stop()


# --- Stale-server / port-collision guards ---
#
# The flake these prevent: a lingering server from a previous run (or a stale
# `just save-bench` supervisor) holds a port; the freshly spawned runbolt dies
# on "address in use"; the readiness probe — and every request after it — is
# cheerfully answered by the WRONG server, so the test asserts against a
# foreign app and fails on missing headers instead of failing on the collision.


@contextlib.contextmanager
def _decoy_server(port: int = 0):
    """A foreign HTTP server that answers 200 `{"status": "ok"}` on every GET.

    Yields the bound port. Binding to a just-freed specific port is retried
    briefly so callers can take over a port whose previous owner was killed.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    deadline = time.time() + 5.0
    while True:
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.server_integration
@pytest.mark.skipif(sys.platform != "linux", reason="listening-socket ownership check is /proc-based")
def test_start_rejects_foreign_server_answering_on_port(make_server_project):
    """Readiness must not be satisfied by a foreign server already on the port.

    runbolt dies on "address in use" while the decoy answers /health; the
    fixture must surface the startup failure instead of handing the test a
    server object wired to the wrong process.
    """
    project = make_server_project()

    with (
        _decoy_server() as port,
        pytest.raises(AssertionError, match="exited before the server became ready|not owned by"),
    ):
        project.start(port=port)


@pytest.mark.server_integration
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX process groups")
def test_request_fails_loudly_when_server_process_died(make_server_project):
    """Requests after the server dies must raise, not hit whoever now owns the port."""
    project = make_server_project()

    with project.start() as server:
        os.killpg(os.getpgid(server.process.pid), signal.SIGKILL)
        deadline = time.time() + 10.0
        while server.process.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert server.process.poll() is not None, "SIGKILL'd runbolt never exited"

        # An imposter takes over the freed port — exactly what a stale bench
        # server does. The harness must refuse to talk to it.
        with _decoy_server(port=server.port), pytest.raises(AssertionError, match="runbolt exited"):
            server.get("/health")


@pytest.mark.skipif(sys.platform == "win32", reason="Process groups are POSIX.")
def test_terminate_process_stops_a_child_that_outlives_its_parent(tmp_path):
    """A child that keeps the output pipes open must not hang the teardown.

    The worker of ``runbolt --dev`` can outlive its supervisor in this way.
    """
    child = "import time; time.sleep(600)"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    process = helpers._spawn_process([sys.executable, "-c", parent], cwd=tmp_path, env=dict(os.environ))
    process.wait(timeout=10)  # The parent exits at once. Its child keeps the pipes.

    done = threading.Event()

    def terminate() -> None:
        helpers._terminate_process(process, timeout=1.0)
        done.set()

    threading.Thread(target=terminate, daemon=True).start()
    try:
        assert done.wait(10), "_terminate_process waited for the pipes of the orphaned child"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def test_parallel_workers_get_disjoint_ports_outside_the_ephemeral_range(monkeypatch):
    """Each pytest-xdist worker hands out server ports from its own block.

    A port from bind(0) is free only until its socket closes. Another worker can
    get the same port before the server binds it. Disjoint blocks below the
    ephemeral range (32768 and up) prevent that.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "8")
    seen = {}
    for worker in ("gw0", "gw1", "gw7"):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        seen[worker] = {helpers.get_free_port() for _ in range(5)}

    assert all(len(ports) == 5 for ports in seen.values())
    assert all(port < 32768 for ports in seen.values() for port in ports)
    assert not seen["gw0"] & seen["gw1"]
    assert not seen["gw0"] & seen["gw7"]
    assert not seen["gw1"] & seen["gw7"]


def test_restarted_worker_gets_ports_outside_the_ephemeral_range(monkeypatch):
    """A worker that pytest-xdist starts again after a crash gets the next id.

    The id of such a worker, for example ``gw4`` of 4, can be the worker count
    or more. Its ports must stay below the ephemeral range.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "4")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")

    ports = {helpers.get_free_port() for _ in range(5)}

    assert all(helpers._WORKER_PORTS.start <= port < 32768 for port in ports), ports


def test_worker_without_a_worker_count_gets_a_port(monkeypatch):
    """Without ``PYTEST_XDIST_WORKER_COUNT``, the OS picks the port."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    monkeypatch.delenv("PYTEST_XDIST_WORKER_COUNT", raising=False)

    assert helpers.get_free_port() > 0


def test_websocket_client_keeps_a_frame_sent_with_the_handshake():
    """A frame in the same read as the 101 response is not lost."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve_once() -> None:
        conn, _ = listener.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", request).group(1)
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            handshake = (
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            # One write: the handshake and the first text frame arrive together.
            conn.sendall(handshake + b"\x81\x02hi")
            conn.recv(4096)

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        with helpers.SimpleWebSocketClient("127.0.0.1", port, "/ws", timeout=2.0) as websocket:
            assert websocket.receive_text() == "hi"
    finally:
        listener.close()
        thread.join(timeout=2.0)

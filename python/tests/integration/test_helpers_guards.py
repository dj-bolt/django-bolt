from __future__ import annotations

import contextlib
import http.server
import os
import signal
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

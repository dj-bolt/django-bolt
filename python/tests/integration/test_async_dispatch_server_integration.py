"""Two-altitude coverage for the production async dispatch path.

Production dispatch only runs in a real ``runbolt`` server — TestClient bridges
through ``run_coroutine_threadsafe``. The subprocess test drives every shape
through the process-lived worker loop.
"""

from __future__ import annotations

import socketserver
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from django_bolt.testing import TestClient

from .apps import app_module
from .apps import dispatch_probes as probes_app

PAYLOAD = {"message": "hello", "n": 1}

EXPECTATIONS = [
    ("/t-sync", 200, PAYLOAD),
    ("/t-trivial", 200, PAYLOAD),
    ("/t-ready", 200, PAYLOAD),
    ("/t-sleep0", 200, PAYLOAD),
    ("/t-timer", 200, PAYLOAD),
    ("/t-thread", 200, {"value": "from-thread"}),
    ("/t-subprocess", 200, {"value": "from-subprocess", "returncode": 0}),
    ("/t-subprocess-wait", 200, {"returncode": 0}),
    ("/t-exc", 418, {"detail": "teapot-after-await"}),
    ("/t-deps", 200, {"a": "a", "b": "b"}),
    ("/t-task", 200, {"loop_running": True, "task_result": "a"}),
    ("/t-call-soon-coroutine", 200, {"raised": "TypeError"}),
]


def _assert_probes(client) -> None:
    for path, status, payload in EXPECTATIONS:
        response = client.get(path)
        assert response.status_code == status, path
        assert response.json() == payload, path

    stream = client.get("/t-stream")
    assert stream.status_code == 200
    assert stream.text == '{"i": 0}\n{"i": 1}\n{"i": 2}\n'


def test_dispatch_probes_in_process():
    with TestClient(probes_app.api) as client:
        _assert_probes(client)


class _EchoHandler(socketserver.StreamRequestHandler):
    # Lines received from the probes; asserted on the main thread because an
    # AssertionError raised here is swallowed by socketserver.handle_error.
    received: ClassVar[list[bytes]] = []
    line_received: ClassVar[threading.Condition] = threading.Condition()

    def handle(self):
        self.wfile.write(b"hello\n")
        line = self.rfile.readline()
        with self.line_received:
            self.received.append(line)
            self.line_received.notify_all()

    @classmethod
    def wait_for_lines(cls, count: int, timeout: float = 5) -> list[bytes]:
        with cls.line_received:
            cls.line_received.wait_for(lambda: len(cls.received) >= count, timeout)
            return list(cls.received)


@pytest.mark.server_integration
def test_dispatch_probes_worker_loop_default(make_server_project):
    """The default loop supports normal asyncio compatibility semantics."""
    project = make_server_project(api_module=app_module("dispatch_probes"))
    certificate = project.path("localhost.crt")
    private_key = project.path("localhost.key")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate, private_key)

    _EchoHandler.received.clear()
    echo_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EchoHandler)
    echo_thread = threading.Thread(target=echo_server.serve_forever, daemon=True)
    echo_thread.start()

    class StartTLSHandler(socketserver.BaseRequestHandler):
        @staticmethod
        def read_line(sock):
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = sock.recv(1)
                if not chunk:
                    break
                data.extend(chunk)
            return bytes(data)

        def handle(self):
            assert self.read_line(self.request) == b"STARTTLS\n"
            self.request.sendall(b"READY\n")
            self.request = tls_context.wrap_socket(self.request, server_side=True)
            assert self.read_line(self.request) == b"over-tls\n"
            self.request.sendall(b"tls-ok\n")

    starttls_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), StartTLSHandler)
    starttls_thread = threading.Thread(target=starttls_server.serve_forever, daemon=True)
    starttls_thread.start()

    class SlowSinkHandler(socketserver.BaseRequestHandler):
        def handle(self):
            header = bytearray()
            while len(header) < 8:
                header.extend(self.request.recv(8 - len(header)))
            expected = int.from_bytes(header, "big")
            time.sleep(0.1)
            received = 0
            while received < expected:
                chunk = self.request.recv(min(65536, expected - received))
                if not chunk:
                    break
                received += len(chunk)
            self.request.sendall(received.to_bytes(8, "big"))

    slow_sink_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SlowSinkHandler)
    slow_sink_thread = threading.Thread(target=slow_sink_server.serve_forever, daemon=True)
    slow_sink_thread.start()

    class HTTPHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b'{"pooled": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
    http_server.socket = tls_context.wrap_socket(http_server.socket, server_side=True)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()

    try:
        with project.start(
            env={
                "BOLT_PROBE_TCP_PORT": str(echo_server.server_address[1]),
                "BOLT_PROBE_STARTTLS_PORT": str(starttls_server.server_address[1]),
                "BOLT_PROBE_SLOW_SINK_PORT": str(slow_sink_server.server_address[1]),
                "BOLT_PROBE_HTTP_URL": f"https://127.0.0.1:{http_server.server_address[1]}/",
            }
        ) as server:
            _assert_probes(server)

            # Selector-backed outbound I/O works on the default WorkerLoop.
            response = server.get("/t-socket")
            assert response.status_code == 200
            assert response.json() == {"greeting": "hello"}
            assert server.get("/t-buffered-protocol").json() == {"greeting": "hello"}
            assert _EchoHandler.wait_for_lines(2) == [b"ping\n", b"ping\n"]
            assert server.get("/t-start-tls").json() == {"reply": "tls-ok"}
            assert server.get("/t-backpressure").json() == {
                "received": 8 * 1024 * 1024,
                "backpressure_observed": True,
            }
            assert server.get("/t-pipes").json() == {"value": "through-pipes"}
            assert server.get("/t-fd-watcher").json() == {
                "value": "fd-ready",
                "reader_removed": True,
                "writer_removed": True,
            }
            assert server.get("/t-signal").json() == {"received": True, "removed": True}
            assert server.get("/t-worker-server").json() == {"reply": "worker-server"}
            server_api = server.get("/t-server-api").json()
            assert server_api["get_loop_is_running_loop"] is True
            if server_api["close_clients_supported"]:
                assert server_api["close_clients_eof"] is True
            else:
                assert server_api["close_clients_eof"] is None
            assert server.get("/t-datagram").json() == {"reply": "datagram-ok"}
            assert server.get("/t-fd-level-triggered").json() == {"chunks": ["a", "b", "c", "d"]}
            assert server.get("/t-fd-cancelled-handle").json() == {"watcher_stopped": True, "calls": 0}
            assert server.get("/t-fd-read-write").json() == {
                "value": "rw",
                "writer_removed": True,
                "reader_removed": True,
                "writer_removed_again": False,
            }
            assert server.get("/t-datagram-flow-control").json() == {"paused": True, "resumed": True}

            # A detached Task survives the response that created it.
            assert server.get("/t-background-start").json() == {"started": True}
            server.wait_for_json("/t-background-status", lambda body: body == {"done": True})

            # Contention binds asyncio.Lock to the loop. Both requests must see
            # the same process-lived facade rather than per-request loop objects.
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(server.get, "/t-shared-lock") for _ in range(2)]
                responses = [future.result() for future in futures]
            assert all(response.status_code == 200 for response in responses)
            assert len({response.json()["loop"] for response in responses}) == 1

            # A real connection pool remains usable when cached across requests.
            assert server.get("/t-cached-client").json() == {"pooled": True}
            with ThreadPoolExecutor(max_workers=8) as executor:
                pooled = list(executor.map(lambda _: server.get("/t-cached-client"), range(32)))
            assert all(response.status_code == 200 for response in pooled)
            assert all(response.json() == {"pooled": True} for response in pooled)

            # A finite-but-oversized delay raises OverflowError from the Rust
            # scheduler instead of panicking (panic = abort in release wheels).
            # WorkerLoop-only: the stdlib loop accepts huge delays, so this is
            # not part of the shared probe expectations.
            assert server.get("/t-oversized-timer").json() == {"raised": "OverflowError"}

            # Cancelled timers are compacted out of the Rust heap rather than
            # retained until their (here: one-hour) deadlines.
            assert server.get("/t-cancelled-timers").json() == {"scheduled": 200}
            server.wait_for_json("/t-timer-stats", lambda body: body["live"] <= 20)
    finally:
        echo_server.shutdown()
        echo_server.server_close()
        echo_thread.join(timeout=2)
        starttls_server.shutdown()
        starttls_server.server_close()
        starttls_thread.join(timeout=2)
        slow_sink_server.shutdown()
        slow_sink_server.server_close()
        slow_sink_thread.join(timeout=2)
        http_server.shutdown()
        http_server.server_close()
        http_thread.join(timeout=2)

"""Integration tests for worker recycling and graceful shutdown.

Covers the real ``runbolt`` server:

- SIGTERM closes active WebSocket connections with 1012 (Service Restart)
  instead of dropping them abruptly.
- ``--workers-lifetime`` recycles workers (new PID) *while the server is under
  concurrent HTTP load*, without dropping requests (spawn-first via
  SO_REUSEPORT).
- ``--max-rss`` recycles a real memory-leaking app under sustained load,
  keeping resident memory bounded while still serving every request.
- ``--respawn-failed-workers`` replaces a crashed worker.

The load-based tests are the teeth behind the "no downtime" claim: a recycle
that silently reset the fleet or dropped in-flight requests would surface here
as non-zero failures. Load is generated in-process with ``httpx`` (see
``helpers.generate_load``) so the suite has no external load-tool dependency.
"""

from __future__ import annotations

import contextlib
import os
import platform
import signal
import time

import httpx
import pytest

from django_bolt.workers import MIB, get_rss_bytes

from .apps import app_module
from .helpers import SimpleWebSocketClient, child_pids, generate_load

pytestmark = pytest.mark.server_integration

RECYCLE_TIMEOUT = 30.0

# child_pids()/get_rss_bytes() rely on POSIX /proc + ps and fork-based workers.
_requires_posix = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="worker recycling under load requires POSIX fork + /proc RSS reading",
)


def _receive_close_skipping_pings(websocket: SimpleWebSocketClient) -> tuple[int, str]:
    """Receive frames until a close frame arrives (heartbeat pings may interleave).

    Pings are answered with pongs (RFC 6455 obliges the client to). On a
    CPU-saturated machine the drain close can arrive later than the server's
    heartbeat client_timeout, and an unanswered ping budget gets the
    connection dropped with a bare TCP FIN instead of the close frame this
    test is waiting for.
    """
    for _ in range(10):
        opcode, payload = websocket._receive_frame()
        if opcode == 0x8:
            if len(payload) >= 2:
                return int.from_bytes(payload[:2], "big"), payload[2:].decode("utf-8")
            return 1005, ""
        assert opcode in (0x9, 0xA), f"Unexpected frame opcode {opcode} while waiting for close"
        if opcode == 0x9:
            websocket._send_frame(0xA, payload)
    raise AssertionError("No close frame received")


def _wait_for_pid_change(server, first_pid: int, timeout: float = RECYCLE_TIMEOUT) -> int:
    payload = server.wait_for_json("/pid", lambda data: data["pid"] != first_pid, timeout=timeout)
    return payload["pid"]


def test_sigterm_closes_websockets_with_service_restart_code(make_server_project):
    project = make_server_project(api_module=app_module("recycling_ws_echo"))

    with (
        project.start() as server,
        SimpleWebSocketClient(server.host, server.port, "/ws/echo") as websocket,
    ):
        websocket.send_text("hello")
        assert websocket.receive_text() == "echo:hello"

        # Signal the server directly (single process == the server itself).
        os.kill(server.process.pid, signal.SIGTERM)

        try:
            code, reason = _receive_close_skipping_pings(websocket)
        except AssertionError as exc:
            # A missing close frame means the server died rudely; its exit
            # status and output say who killed it (e.g. -9 = SIGKILLed from
            # outside, -15 = SIGTERM never reached the graceful drain path).
            deadline = time.time() + 5.0
            while server.process.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            stdout, stderr = server.stop()
            raise AssertionError(
                f"{exc}\nserver exit code: {server.process.returncode}"
                f"\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ) from exc

    assert code == 1012, f"Expected 1012 Service Restart close, got {code} ({reason!r})"

    # The server must also actually exit (graceful stop, not a hang).
    deadline = time.time() + 15.0
    while server.process.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    exit_code = server.process.poll()
    assert exit_code is not None, "Server did not exit after SIGTERM"
    stdout, stderr = server.stop()
    # -6 here means a Rust panic aborted the process during shutdown.
    assert exit_code == 0, f"Graceful SIGTERM shutdown must exit 0, got {exit_code}\nstderr:\n{stderr}"


def test_workers_lifetime_recycles_worker_without_downtime(make_server_project):
    project = make_server_project(api_module=app_module("recycling_pid"))

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


@_requires_posix
def test_workers_lifetime_recycles_under_load_without_dropping_requests(make_server_project):
    """Recycle repeatedly *while under concurrent load* and drop nothing.

    A short lifetime against a two-worker fleet forces several spawn-first
    recycles during the load window. Every request must still succeed — a
    dropped connection or a gap with no accepting process would show up as a
    non-zero failure count.
    """
    project = make_server_project(api_module=app_module("recycling_pid"))

    with project.start(
        processes=2,
        extra_args=["--workers-lifetime", "2", "--workers-kill-timeout", "5"],
    ) as server:
        seen_workers: set[int] = set()
        seen_workers.update(child_pids(server.process.pid))

        result = generate_load(
            server.url("/health"),
            duration_s=10.0,
            concurrency=16,
            on_tick=lambda: seen_workers.update(child_pids(server.process.pid)),
        )

        recycles = len(seen_workers) - 2  # started with 2 workers
        assert recycles >= 1, f"expected at least one recycle under load, saw {seen_workers}"
        assert result.ok > 1000, f"load generator barely ran: {result.ok} ok requests"
        # failed == request unserved after retries (true downtime). retried > 0 is
        # expected and fine: graceful drain closes idle keep-alive connections and
        # the client reconnects to a live worker.
        assert result.failed == 0, (
            f"requests were dropped during recycling: {result.failed}/{result.total} "
            f"failed after retries (retried={result.retried}, errors={result.errors})"
        )


@_requires_posix
def test_max_rss_recycles_leaking_app_under_load_with_bounded_memory(make_server_project):
    """A real leaking app under load: --max-rss recycles it, memory stays bounded.

    Bombard a two-worker fleet whose ``/leak`` endpoint retains memory on every
    request. Without recycling, worker RSS climbs monotonically; with ``--max-rss``
    the supervisor recycles each worker as it crosses the limit, so a fresh
    replacement (RSS back near baseline) takes over. The test asserts:

    - recycling actually happens under load (worker PIDs rotate),
    - recycling *bounds* memory: a worker is observed back near baseline RSS in
      the second half of the run. This can only happen if a grown worker was
      replaced by a fresh one — without recycling every worker would be far above
      baseline by then, so this assertion fails if recycling is disabled/broken
      (verified via a no-recycle control), and
    - not a single request is dropped despite the constant churn.
    """
    project = make_server_project(api_module=app_module("recycling_leak"))

    # Measure a fresh worker's baseline RSS so the limit is above idle memory —
    # this exercises the *leak-driven* recycle path, not an immediate trip.
    # Single-process mode runs the server in-process (no forked children), so
    # read that process's own RSS.
    with project.start(processes=1) as probe:
        probe.get("/health")
        time.sleep(0.5)
        baseline_bytes = get_rss_bytes(probe.process.pid)
    assert baseline_bytes, "could not read baseline worker RSS"
    baseline_mib = baseline_bytes / MIB

    limit_mib = int(baseline_mib + 40)
    duration = 14.0

    with project.start(
        processes=2,
        extra_args=["--max-rss", str(limit_mib), "--workers-kill-timeout", "3"],
    ) as server:
        start = time.monotonic()
        seen_workers: set[int] = set()
        peak_rss = 0.0
        # Smallest worker RSS seen in the *second half* of the run. A recycle
        # resets a worker to ~baseline; without recycling this stays high.
        min_rss_late = float("inf")

        def sample() -> None:
            nonlocal peak_rss, min_rss_late
            seen_workers.update(child_pids(server.process.pid))
            current = [get_rss_bytes(pid) for pid in child_pids(server.process.pid)]
            live = [rss / MIB for rss in current if rss]
            if live:
                peak_rss = max(peak_rss, max(live))
                if time.monotonic() - start > duration / 2:
                    min_rss_late = min(min_rss_late, min(live))

        result = generate_load(
            server.url("/leak"),
            duration_s=duration,
            concurrency=16,
            on_tick=sample,
        )

        recycles = len(seen_workers) - 2
        assert recycles >= 2, (
            f"leak did not drive recycles under load (limit={limit_mib}MiB, "
            f"baseline={baseline_mib:.0f}MiB, workers seen={seen_workers})"
        )
        # A worker was reset to ~baseline after growing => recycling caps memory.
        assert min_rss_late < baseline_mib + 25, (
            f"no worker returned near baseline in the second half "
            f"(min late RSS {min_rss_late:.0f}MiB, baseline {baseline_mib:.0f}MiB, "
            f"peak {peak_rss:.0f}MiB) — recycling is not bounding memory"
        )
        assert result.ok > 1000, f"load generator barely ran: {result.ok} ok requests"
        assert result.failed == 0, (
            f"requests were dropped while recycling a leaking app: "
            f"{result.failed}/{result.total} failed after retries "
            f"(retried={result.retried}, errors={result.errors})"
        )


def test_max_rss_recycles_worker(make_server_project):
    project = make_server_project(api_module=app_module("recycling_pid"))

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
    project = make_server_project(api_module=app_module("recycling_pid"))

    with project.start(extra_args=["--respawn-failed-workers"]) as server:
        first_pid = server.get("/pid").json()["pid"]

        # os._exit(1) kills the worker mid-request; the connection drops.
        with contextlib.suppress(httpx.HTTPError):
            server.get("/die")

        new_pid = _wait_for_pid_change(server, first_pid)
        assert new_pid != first_pid
        assert server.get("/health").json() == {"status": "ok"}

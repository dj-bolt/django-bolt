"""Worker process supervision for ``runbolt``.

Long-running Python servers accumulate resident memory (leaky C extensions,
allocator fragmentation, unbounded caches). Traditional servers work around
this with ``max_requests``-style restarts; request count is only a proxy for
memory, so — like Granian — django-bolt recycles workers on the actual
symptoms instead:

- ``max_rss_bytes``: recycle a worker once its resident set size exceeds a
  threshold (the primary defense against slow memory leaks).
- ``lifetime_seconds``: recycle workers after a wall-clock lifetime.
- ``respawn_failed``: replace workers that exit unexpectedly.

Recycling is *graceful and spawn-first*: a replacement worker is forked and
binds the same port via SO_REUSEPORT *before* the old worker receives
SIGTERM, so the port is never left without an accepting process. On SIGTERM
the worker stops accepting connections, closes WebSocket connections with
code 1012 (Service Restart) so clients reconnect to a healthy worker,
finishes in-flight requests, and exits. A worker that has not exited after
``kill_timeout`` seconds is SIGKILLed.

All OS interactions (spawn, kill, waitpid, RSS reading, clock, sleep) are
injectable for deterministic unit testing.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

MIB = 1024 * 1024

DEFAULT_KILL_TIMEOUT = 30.0
DEFAULT_CHECK_INTERVAL = 1.0


def get_rss_bytes(pid: int) -> int | None:
    """Return the resident set size of *pid* in bytes, or None if unreadable.

    Reads ``/proc/<pid>/status`` (Linux) and falls back to ``ps -o rss=``
    (macOS/BSD). Returns None for dead PIDs or unsupported platforms.
    """
    try:
        with open(f"/proc/{pid}/status", "rb") as status_file:
            for line in status_file:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1]) * 1024  # VmRSS is in KiB
    except (OSError, ValueError, IndexError):
        pass

    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return int(value) * 1024  # ps reports RSS in KiB
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return None


@dataclass
class _Worker:
    slot: int
    pid: int
    started_at: float


class WorkerSupervisor:
    """Supervises forked worker processes: recycling, respawn, shutdown.

    The supervisor owns the parent process main loop. ``spawn(slot)`` must
    fork a worker for the given slot and return its PID (in the parent).
    Call :meth:`request_shutdown` from a signal handler to begin graceful
    shutdown; a second call escalates to SIGKILL.
    """

    def __init__(
        self,
        *,
        processes: int,
        spawn: Callable[[int], int],
        max_rss_bytes: int | None = None,
        lifetime_seconds: float | None = None,
        respawn_failed: bool = False,
        kill_timeout: float = DEFAULT_KILL_TIMEOUT,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        kill: Callable[[int, int], None] = os.kill,
        waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
        rss_reader: Callable[[int], int | None] = get_rss_bytes,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self.processes = processes
        self.max_rss_bytes = max_rss_bytes
        self.lifetime_seconds = lifetime_seconds
        self.respawn_failed = respawn_failed
        self.kill_timeout = kill_timeout
        self.check_interval = check_interval

        self._spawn = spawn
        self._kill = kill
        self._waitpid = waitpid
        self._rss_reader = rss_reader
        self._clock = clock
        self._log = log

        self._workers: dict[int, _Worker] = {}  # pid -> worker
        self._draining: dict[int, float] = {}  # pid -> SIGKILL deadline
        self._shutdown_requested = False
        self._shutdown_signaled = False
        self._force_kill = False
        # Interruptible sleep: request_shutdown() wakes the loop immediately
        # instead of waiting out the current check interval.
        self._wakeup = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Fork the initial set of workers, one per slot."""
        for slot in range(self.processes):
            self._spawn_slot(slot)

    def run(self) -> None:
        """Supervise until all workers have exited.

        Without ``respawn_failed`` the loop also ends when every worker has
        died on its own (matching the historical runbolt behavior of the
        parent exiting once all children are gone).
        """
        self.start()
        while self._workers or self._draining:
            self._wakeup.wait(self.check_interval)
            self._wakeup.clear()
            self.tick()

    def tick(self) -> None:
        """One supervision pass: reap, then recycle/terminate as needed."""
        self._reap()
        now = self._clock()
        if self._shutdown_requested:
            self._begin_shutdown()
        else:
            self._check_limits(now)
        self._enforce_deadlines(now)

    def request_shutdown(self) -> None:
        """Begin graceful shutdown; a second call escalates to SIGKILL."""
        if self._shutdown_requested:
            self._force_kill = True
        self._shutdown_requested = True
        self._wakeup.set()

    def active_worker_count(self) -> int:
        return len(self._workers)

    def has_draining_workers(self) -> bool:
        return bool(self._draining)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn_slot(self, slot: int) -> None:
        pid = self._spawn(slot)
        self._workers[pid] = _Worker(slot=slot, pid=pid, started_at=self._clock())

    def _reap(self) -> None:
        """Collect exited children without blocking."""
        while True:
            try:
                pid, status = self._waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError as exc:
                if exc.errno == errno.ECHILD:
                    return
                raise
            if pid == 0:
                return
            self._on_worker_exit(pid, status)

    def _on_worker_exit(self, pid: int, status: int) -> None:
        if pid in self._draining:
            del self._draining[pid]
            return

        worker = self._workers.pop(pid, None)
        if worker is None:
            return

        if self._shutdown_requested:
            return

        exit_code = os.waitstatus_to_exitcode(status)
        if self.respawn_failed:
            self._log(f"Worker {pid} (slot {worker.slot}) exited unexpectedly (code {exit_code}); respawning")
            self._spawn_slot(worker.slot)
        else:
            self._log(
                f"Worker {pid} (slot {worker.slot}) exited unexpectedly (code {exit_code}); "
                f"not respawning (enable with --respawn-failed-workers)"
            )

    def _check_limits(self, now: float) -> None:
        for worker in list(self._workers.values()):
            reason = self._limit_violation(worker, now)
            if reason is not None:
                self._recycle(worker, reason)
                # At most one recycle per tick: staggers respawns when every
                # worker crosses a limit at once (common with a shared leak),
                # so capacity never drops to all-cold workers simultaneously.
                return

    def _limit_violation(self, worker: _Worker, now: float) -> str | None:
        if self.lifetime_seconds is not None and now - worker.started_at >= self.lifetime_seconds:
            return f"lifetime {self.lifetime_seconds:.0f}s reached"
        if self.max_rss_bytes is not None:
            rss = self._rss_reader(worker.pid)
            if rss is not None and rss > self.max_rss_bytes:
                return f"rss {rss / MIB:.0f}MiB exceeds limit {self.max_rss_bytes / MIB:.0f}MiB"
        return None

    def _recycle(self, worker: _Worker, reason: str) -> None:
        self._log(f"Recycling worker {worker.pid} (slot {worker.slot}): {reason}")
        del self._workers[worker.pid]
        # Spawn the replacement BEFORE stopping the old worker so the port
        # always has an accepting process (SO_REUSEPORT allows the overlap).
        self._spawn_slot(worker.slot)
        self._terminate(worker.pid)

    def _terminate(self, pid: int) -> None:
        self._draining[pid] = self._clock() + self.kill_timeout
        # ProcessLookupError: already dead; the next reap pass clears it.
        with contextlib.suppress(ProcessLookupError):
            self._kill(pid, signal.SIGTERM)

    def _begin_shutdown(self) -> None:
        if not self._shutdown_signaled:
            self._shutdown_signaled = True
            worker_count = len(self._workers)
            if worker_count:
                self._log(f"Shutting down {worker_count} worker{'s' if worker_count != 1 else ''}...")
            for pid in list(self._workers):
                del self._workers[pid]
                self._terminate(pid)
        if self._force_kill:
            for pid in self._draining:
                self._draining[pid] = 0.0

    def _enforce_deadlines(self, now: float) -> None:
        for pid, deadline in list(self._draining.items()):
            if now >= deadline:
                self._log(f"Worker {pid} did not exit within kill timeout; sending SIGKILL")
                with contextlib.suppress(ProcessLookupError):
                    self._kill(pid, signal.SIGKILL)
                # Push the deadline out so a slow reap doesn't re-SIGKILL.
                self._draining[pid] = now + self.kill_timeout

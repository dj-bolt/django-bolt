"""Unit tests for the runbolt worker supervisor (RSS/lifetime recycling).

These tests drive :class:`django_bolt.workers.WorkerSupervisor` deterministically
through injected fakes (clock, spawn, kill, waitpid, RSS reader) — no real
processes are forked. Real-server behavior is covered by
``integration/test_worker_recycling_integration.py``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from django_bolt.workers import WorkerSupervisor, get_rss_bytes

MIB = 1024 * 1024


class FakeWorkerEnv:
    """Deterministic stand-in for the OS process APIs the supervisor uses.

    Records every spawn and signal into a single ``events`` list so tests can
    assert ordering (e.g. replacement spawned *before* the old worker is
    terminated).
    """

    def __init__(self):
        self.now = 0.0
        self.next_pid = 100
        self.alive: set[int] = set()
        self.exited: list[tuple[int, int]] = []
        self.rss: dict[int, int | None] = {}
        self.events: list[tuple] = []

    def spawn(self, slot: int) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.alive.add(pid)
        self.events.append(("spawn", slot, pid))
        return pid

    def kill(self, pid: int, signum: int) -> None:
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        self.events.append(("signal", pid, signum))
        if signum == signal.SIGKILL:
            self.exit(pid, -signal.SIGKILL)

    def exit(self, pid: int, status: int = 0) -> None:
        if pid in self.alive:
            self.alive.remove(pid)
            self.exited.append((pid, status))

    def waitpid(self, pid: int, flags: int) -> tuple[int, int]:
        if self.exited:
            return self.exited.pop(0)
        if not self.alive:
            raise ChildProcessError
        return (0, 0)

    def clock(self) -> float:
        return self.now

    def rss_reader(self, pid: int) -> int | None:
        return self.rss.get(pid)

    def signals(self) -> list[tuple[int, int]]:
        return [(pid, signum) for kind, pid, signum in self.events if kind == "signal"]

    def spawns(self) -> list[tuple[int, int]]:
        return [(slot, pid) for kind, slot, pid in self.events if kind == "spawn"]


def make_supervisor(env: FakeWorkerEnv, **kwargs) -> WorkerSupervisor:
    defaults = {
        "processes": 1,
        "spawn": env.spawn,
        "kill": env.kill,
        "waitpid": env.waitpid,
        "rss_reader": env.rss_reader,
        "clock": env.clock,
        "kill_timeout": 10.0,
        "log": lambda _message: None,
    }
    defaults.update(kwargs)
    return WorkerSupervisor(**defaults)


def test_start_spawns_one_worker_per_slot():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, processes=3)
    supervisor.start()
    assert env.spawns() == [(0, 100), (1, 101), (2, 102)]
    assert supervisor.active_worker_count() == 3


def test_rss_over_limit_spawns_replacement_before_terminating_old_worker():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, max_rss_bytes=100 * MIB)
    supervisor.start()
    old_pid = env.spawns()[0][1]

    env.rss[old_pid] = 200 * MIB
    supervisor.tick()

    # Replacement forked first (port stays covered via SO_REUSEPORT),
    # SIGTERM to the old worker second.
    assert env.events == [
        ("spawn", 0, old_pid),
        ("spawn", 0, old_pid + 1),
        ("signal", old_pid, signal.SIGTERM),
    ]


def test_rss_under_limit_does_not_recycle():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, max_rss_bytes=100 * MIB)
    supervisor.start()
    env.rss[100] = 50 * MIB
    supervisor.tick()
    assert env.signals() == []
    assert len(env.spawns()) == 1


def test_recycled_worker_exit_is_reaped_without_respawn():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, max_rss_bytes=100 * MIB, respawn_failed=True)
    supervisor.start()
    env.rss[100] = 200 * MIB
    supervisor.tick()

    # Old worker exits gracefully: reaped, no extra spawn beyond the replacement.
    env.exit(100, 0)
    supervisor.tick()
    assert len(env.spawns()) == 2
    assert supervisor.active_worker_count() == 1
    assert not supervisor.has_draining_workers()


def test_draining_worker_gets_sigkill_after_kill_timeout():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, max_rss_bytes=100 * MIB, kill_timeout=10.0)
    supervisor.start()
    env.rss[100] = 200 * MIB
    supervisor.tick()
    assert (100, signal.SIGTERM) in env.signals()

    # Worker refuses to exit: after the kill timeout it is SIGKILLed.
    env.now = 9.0
    supervisor.tick()
    assert (100, signal.SIGKILL) not in env.signals()
    env.now = 11.0
    supervisor.tick()
    assert (100, signal.SIGKILL) in env.signals()


def test_lifetime_exceeded_recycles_worker():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, lifetime_seconds=60.0)
    supervisor.start()

    env.now = 59.0
    supervisor.tick()
    assert env.signals() == []

    env.now = 61.0
    supervisor.tick()
    assert env.spawns() == [(0, 100), (0, 101)]
    assert env.signals() == [(100, signal.SIGTERM)]


def test_at_most_one_recycle_per_tick():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, processes=2, max_rss_bytes=100 * MIB)
    supervisor.start()
    env.rss[100] = 200 * MIB
    env.rss[101] = 200 * MIB

    supervisor.tick()
    assert len(env.signals()) == 1

    supervisor.tick()
    assert len(env.signals()) == 2


def test_unexpected_exit_respawns_when_enabled():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, respawn_failed=True)
    supervisor.start()

    env.exit(100, 1)
    supervisor.tick()
    assert env.spawns() == [(0, 100), (0, 101)]
    assert supervisor.active_worker_count() == 1


def test_unexpected_exit_not_respawned_when_disabled():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, respawn_failed=False)
    supervisor.start()

    env.exit(100, 1)
    supervisor.tick()
    assert env.spawns() == [(0, 100)]
    assert supervisor.active_worker_count() == 0


def test_shutdown_terminates_all_workers_and_reaps():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, processes=2)
    supervisor.start()

    supervisor.request_shutdown()
    supervisor.tick()
    assert sorted(env.signals()) == [(100, signal.SIGTERM), (101, signal.SIGTERM)]

    env.exit(100, 0)
    env.exit(101, 0)
    supervisor.tick()
    assert supervisor.active_worker_count() == 0
    assert not supervisor.has_draining_workers()


def test_shutdown_does_not_respawn_exiting_workers():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, respawn_failed=True)
    supervisor.start()

    supervisor.request_shutdown()
    env.exit(100, 0)
    supervisor.tick()
    assert env.spawns() == [(0, 100)]


def test_second_shutdown_request_forces_sigkill():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, kill_timeout=30.0)
    supervisor.start()

    supervisor.request_shutdown()
    supervisor.tick()
    assert env.signals() == [(100, signal.SIGTERM)]

    supervisor.request_shutdown()  # e.g. second Ctrl-C
    supervisor.tick()
    assert (100, signal.SIGKILL) in env.signals()


def test_rss_reader_failure_is_not_a_recycle():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, max_rss_bytes=100 * MIB)
    supervisor.start()
    env.rss[100] = None  # e.g. /proc read failed
    supervisor.tick()
    assert env.signals() == []


@pytest.mark.skipif(sys.platform.startswith("win"), reason="RSS reading is POSIX-only")
def test_get_rss_bytes_reads_own_process():
    rss = get_rss_bytes(os.getpid())
    assert rss is not None
    assert rss > MIB  # a running CPython interpreter is always > 1 MiB resident


def test_get_rss_bytes_returns_none_for_dead_pid():
    # Spawn a child that exits immediately; after wait() reaps it, the PID is gone.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert get_rss_bytes(child.pid) is None

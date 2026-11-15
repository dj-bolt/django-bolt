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


@pytest.mark.parametrize(
    ("limits", "age", "rss", "recycled"),
    [
        ({"lifetime_seconds": 60.0}, 59.9, None, False),
        ({"lifetime_seconds": 60.0}, 60.0, None, True),
        ({"max_rss_bytes": 100 * MIB}, 0.0, 100 * MIB, False),
        ({"max_rss_bytes": 100 * MIB}, 0.0, 100 * MIB + 1, True),
    ],
)
def test_a_worker_is_recycled_at_its_lifetime_and_above_its_rss_limit(limits, age, rss, recycled):
    env = FakeWorkerEnv()
    env.now = 1000.0  # The clock of the supervisor does not start at 0.
    supervisor = make_supervisor(env, **limits)
    supervisor.start()

    env.now += age
    env.rss[100] = rss
    supervisor.tick()

    assert env.signals() == ([(100, signal.SIGTERM)] if recycled else [])


def test_a_worker_that_survives_sigkill_gets_one_sigkill_per_kill_timeout():
    env = FakeWorkerEnv()
    signals: list[tuple[int, int]] = []
    # This kill records the signal and the worker stays. FakeWorkerEnv.kill ends the worker on SIGKILL.
    supervisor = make_supervisor(env, kill_timeout=10.0, kill=lambda pid, signum: signals.append((pid, signum)))
    supervisor.start()
    supervisor.request_shutdown()
    supervisor.tick()

    env.now = 10.0
    supervisor.tick()
    supervisor.tick()
    env.now = 19.9
    supervisor.tick()
    assert signals == [(100, signal.SIGTERM), (100, signal.SIGKILL)]
    assert supervisor.has_draining_workers()

    env.now = 20.0
    supervisor.tick()
    assert signals == [(100, signal.SIGTERM), (100, signal.SIGKILL), (100, signal.SIGKILL)]


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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/statm is the Linux ground truth")
def test_get_rss_bytes_reads_proc_in_bytes(monkeypatch, tmp_path):
    # No `ps` on the PATH: only the /proc reader can give a value.
    monkeypatch.setenv("PATH", str(tmp_path))

    rss = get_rss_bytes(os.getpid())
    with open("/proc/self/statm") as statm:
        resident_pages = int(statm.read().split()[1])
    expected = resident_pages * os.sysconf("SC_PAGE_SIZE")

    assert rss is not None, "the /proc reader gave no value, and the ps fallback hid it"
    # The two reads are not at the same instant. A wrong unit is off by a factor of 1024.
    assert expected * 0.8 <= rss <= expected * 1.25


@pytest.mark.skipif(sys.platform.startswith("win"), reason="RSS reading is POSIX-only")
def test_get_rss_bytes_follows_the_memory_of_a_process():
    code = "import sys; block = b'x' * (64 * 1024 * 1024); print('ready', flush=True); sys.stdin.read()"
    small = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    large = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert small.stdout.readline() == b"ready\n"
        assert large.stdout.readline() == b"ready\n"
        difference = get_rss_bytes(large.pid) - get_rss_bytes(small.pid)
    finally:
        for process in (small, large):
            process.communicate(b"")

    # The large process holds 64 MiB more. A value in KiB or in pages is far below this range.
    assert 48 * MIB <= difference <= 96 * MIB


def test_get_rss_bytes_returns_none_for_dead_pid():
    # Spawn a child that exits immediately; after wait() reaps it, the PID is gone.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert get_rss_bytes(child.pid) is None


def _spawn_then_exit(env: FakeWorkerEnv, status: int):
    def spawn(slot: int) -> int:
        pid = env.spawn(slot)
        env.exit(pid, status)
        return pid

    return spawn


def test_run_fails_when_every_worker_exits_with_an_error():
    """A process manager must see the failure, for example ``Restart=on-failure`` in systemd."""
    env = FakeWorkerEnv()
    exit_code_1 = 1 << 8  # the wait status of a process that called exit(1)
    supervisor = make_supervisor(env, processes=2, spawn=_spawn_then_exit(env, exit_code_1), check_interval=0.01)

    assert supervisor.run() == 1


def test_run_succeeds_when_the_workers_exit_cleanly_on_their_own():
    env = FakeWorkerEnv()
    supervisor = make_supervisor(env, processes=2, spawn=_spawn_then_exit(env, 0), check_interval=0.01)

    assert supervisor.run() == 0


def test_run_succeeds_after_a_requested_shutdown():
    env = FakeWorkerEnv()

    def kill_then_exit(pid: int, signum: int) -> None:
        env.kill(pid, signum)
        env.exit(pid, 1 << 8)

    supervisor = make_supervisor(env, processes=2, kill=kill_then_exit, check_interval=0.01)
    supervisor.request_shutdown()

    assert supervisor.run() == 0


def test_run_succeeds_when_a_shutdown_follows_a_worker_failure():
    """A stop request after a worker failed is a clean stop, for example ``systemctl stop``."""
    env = FakeWorkerEnv()
    exit_code_1 = 1 << 8
    supervisor: WorkerSupervisor | None = None

    def spawn(slot: int) -> int:
        pid = env.spawn(slot)
        if slot == 0:
            env.exit(pid, exit_code_1)
        return pid

    def kill_then_exit(pid: int, signum: int) -> None:
        env.kill(pid, signum)
        env.exit(pid, 0)

    def log(message: str) -> None:
        # The supervisor logs the failure of slot 0, then the stop request arrives.
        if "exited unexpectedly" in message:
            supervisor.request_shutdown()

    supervisor = make_supervisor(env, processes=2, spawn=spawn, kill=kill_then_exit, log=log, check_interval=0.01)

    assert supervisor.run() == 0


def _spawn_until(env: FakeWorkerEnv, count: int):
    """Spawn ``count`` workers, then fail, as runbolt does when a pool of the parent does not close."""

    def spawn(slot: int) -> int:
        if len(env.spawns()) == count:
            raise RuntimeError("a pool of the parent did not close")
        return env.spawn(slot)

    return spawn


def _kill_then_exit(env: FakeWorkerEnv):
    def kill(pid: int, signum: int) -> None:
        env.kill(pid, signum)
        env.exit(pid, 0)

    return kill


def test_a_failed_spawn_at_start_stops_the_workers_that_run():
    """The parent exits with the error. A worker that it leaves behind serves with no supervisor."""
    env = FakeWorkerEnv()
    supervisor = make_supervisor(
        env, processes=2, spawn=_spawn_until(env, 1), kill=_kill_then_exit(env), check_interval=0.01
    )

    with pytest.raises(RuntimeError, match="did not close"):
        supervisor.run()

    assert env.signals() == [(100, signal.SIGTERM)]
    assert not env.alive


def test_a_failed_spawn_of_a_replacement_stops_the_old_worker():
    """A recycle spawns the replacement first. When that spawn fails, the old worker must still stop."""
    env = FakeWorkerEnv()
    supervisor = make_supervisor(
        env, max_rss_bytes=100 * MIB, spawn=_spawn_until(env, 1), kill=_kill_then_exit(env), check_interval=0.01
    )
    env.rss[100] = 200 * MIB

    with pytest.raises(RuntimeError, match="did not close"):
        supervisor.run()

    assert env.signals() == [(100, signal.SIGTERM)]
    assert not env.alive

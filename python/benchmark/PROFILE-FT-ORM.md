# Free-threaded database endpoints: pool size and executor life cycle

Measured on 2026-09-19. This investigation profiles `/users/full10`, `/users/mini10`, `/auth/me`, and `/middleware/demo`.

**The free-threaded (FT) interpreter is not the cause of the large gaps in `BENCHMARK-FT.md`.**
The two reports compare different topologies.
`BENCHMARK.md` uses 8 processes, so it has 8 ORM pool threads and 8 GILs.
`BENCHMARK-FT.md` uses 1 process, so it has 1 ORM pool thread.

In one process, FT is faster than the GIL build on every measured route.
The speedup is 1.5 to 12 times.

## Throughput matrix

Each cell shows the median requests per second of three four-second runs at concurrency 100.
The value in parentheses is the server CPU use in cores.
All runs use the same source tree and the same extension build date.

| Runtime and topology | ORM threads | `/users/full10` | `/users/sync-full10` | `/users/mini10` | `/auth/me` | `/middleware/demo` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| FT 3.14, 1 process × 12 workers | 1 (default) | 4,771 (1.8) | 21,955 (10.6) | 6,836 (2.1) | 19,039 (3.5) | 3,174 (7.4) |
| FT 3.14, 1 × 12 | 4 | 14,689 (6.5) | 21,391 (10.4) | 18,794 (7.1) | 32,693 (8.9) | 3,334 (7.6) |
| FT 3.14, 1 × 12 | 8 | 20,357 (10.2) | 21,600 (10.5) | 25,834 (10.3) | 29,153 (9.1) | 3,322 (7.5) |
| FT 3.14, 1 × 12 | 12 | 21,390 (10.7) | 21,519 (10.6) | 26,101 (10.4) | 28,219 (9.1) | 3,301 (7.7) |
| GIL 3.14, 1 × 12 | 1 (default) | 3,260 (1.3) | 1,780 (2.4) | 3,839 (1.3) | 8,490 (1.6) | 702 (1.9) |
| GIL 3.14, 1 × 12 | 8 | 1,907 (2.3) | 1,753 (2.4) | 2,555 (2.1) | 6,270 (2.0) | 707 (1.9) |
| GIL 3.14, 8 × 1 | 1 per process | 20,799 (8.6) | 14,466 (9.2) | 26,355 (8.6) | 39,135 (7.3) | 6,484 (8.2) |
| GIL 3.12, 8 × 1 | 1 per process | 20,664 (8.6) | 15,465 (9.1) | 25,911 (8.5) | 39,428 (7.3) | 6,267 (8.1) |
| FT 3.14, 8 × 1 | 1 per process | 21,683 (10.4) | 20,378 (10.8) | 27,884 (10.4) | 31,890 (8.2) | 3,931 (9.1) |

The matrix supports these statements:

- Same topology, 1 × 12: FT is 1.5 times faster on async ORM, 12 times faster on sync ORM, and 4.5 times faster on middleware.
- Same topology, 8 × 1: FT is equal on async ORM, 1.4 times faster on sync ORM, and slower on `/auth/me` and middleware.
- Python 3.12 and Python 3.14 GIL builds give the same results. The Python version does not explain the gaps.
- One FT process with 8 ORM threads equals 8 GIL processes on async ORM.

## Finding 1: one ORM thread serves the full FT process

The example project uses SQLite.
For SQLite, `_default_orm_workers()` in [concurrency.py](../django_bolt/concurrency.py) sets the ORM pool to one thread.
That default came from a GIL measurement: more SQLite connections in one GIL process decreased throughput.
The GIL rows above reproduce that result (3,260 with 1 thread, 1,907 with 8 threads).
On FT the result reverses (4,771 with 1 thread, 20,357 with 8 threads).

Eight GDB snapshots of the FT server under `/users/full10` load show the mechanism.
88 of 96 Actix worker samples were idle in `epoll_wait`.
The single `bolt_orm` thread was busy in every snapshot.
The server used only 1.8 cores.

The latency profile agrees. With one ORM thread, p50 is 20.8 ms and p99 is 24.7 ms.
A narrow, high latency band is the mark of one queue with one server.
With 8 ORM threads, p50 decreases to 4.6 ms, equal to the GIL 8 × 1 result (4.7 ms).

## Finding 2: `/auth/me` blocks the worker thread on the ORM pool

The handler is `async`, but it reads the lazy `request.user` synchronously.
That read calls `run_orm_blocking`, which blocks the Actix worker thread until the ORM thread replies.

The in-process Python sampler recorded 3,803 thread samples.
2,464 samples were in this stack:

```text
Condition.wait
  Future.result
    concurrency._submit_blocking
      concurrency.run_orm_blocking
        user_loader.load_via_sync
          SimpleLazyObject._setup
            api.get_me
```

GDB shows the same state: 73 of 96 worker samples were in a futex wait below `task_step`.
All 12 workers wait for one ORM thread, so the throughput stays at 19k.
With 4 ORM threads the throughput is 32.7k.
More ORM threads then decrease it to 28–29k. This investigation did not find the cause of that decrease.
FT 8 × 1 reaches 31.9k against 39.1k for GIL 8 × 1. This part of the gap remains open.

## Finding 3: the middleware path creates one executor per request

`_run_request_affine` in [django_adapter.py](../django_bolt/middleware/django_adapter.py) opens one asgiref `ThreadSensitiveContext` per request.
asgiref creates one `ThreadPoolExecutor` and one thread for that context.
It shuts the executor down when the request ends.

The Python sampler on `/middleware/demo` recorded these worker stacks:

| Samples | Stack |
| ---: | --- |
| 1,077 | `ThreadPoolExecutor.submit:200` from `SyncToAsync.__call__` |
| 383 | `Thread.join` from `ThreadPoolExecutor.shutdown` from `ThreadSensitiveContext.__aexit__` |

Line 200 is `with self._shutdown_lock, _global_shutdown_lock:`.
`_global_shutdown_lock` is one lock for the full process, so all 12 workers contend for it.
`Thread.join` runs on the event loop thread, so the worker cannot serve other requests during the join.
GDB confirms both: 35 worker samples waited in `lock_PyThread_acquire_lock`, and 12 waited in `ThreadHandle_join`.
GDB also found executor threads in `mi_heap_collect_ex` below `PyThreadState_Clear`. FT gives each thread its own allocator heap, and thread exit must collect it.

A separate probe measures this life cycle alone. Each thread repeats: create a one-thread `ThreadPoolExecutor`, submit one call, shut down.

| Runtime | Threads | Cycles per second | CPU cores used |
| --- | ---: | ---: | ---: |
| FT 3.14 | 1 | 4,295 | 1.25 |
| FT 3.14 | 12 | 12,192 | 3.64 |
| GIL 3.14 | 1 | 12,108 | 1.19 |
| GIL 3.14 | 12 | 9,252 | 1.44 |

One executor cycle is 2.8 times slower on FT.
Twelve FT threads give only 2.8 times the rate of one thread.
Eight GIL processes do not share the lock, so their capacity is about 8 × 12k cycles per second.
This explains why FT loses on this route in both topologies (3.2k and 3.9k against 6.5k).
The per-request executor came with the request-affine middleware change (PR #338).

## Limits

The machine has a Ryzen 5 5600 with 6 physical cores and 12 hardware threads.
The server and the load generator shared the machine without CPU affinity.
Desktop applications also ran. Treat differences below 10% as noise.
The database is SQLite. A networked database has a different ORM pool default (4 threads) and different lock behavior.
GDB counts are stack observations, not percentages of CPU time.
py-spy 0.4.2 cannot read a free-threaded interpreter, so the Python sampler runs inside the server.
All 135 matrix measurement runs reported zero error responses.

## Recommended actions

1. Compare equal topologies in the reports. Add a GIL `1 × 12` run, or an FT `8 × 1` run, next to the current runs.
2. Make the SQLite pool default depend on the runtime. Use one thread with the GIL. Use a larger default when `sys._is_gil_enabled()` is false.
3. Keep one long-lived request-affine thread set for the middleware path, and stop the per-request executor life cycle. This change also helps the GIL build.
4. Investigate the remaining `/auth/me` gap after action 2.

This investigation did not change production code or the original benchmark reports.

## Method

A driver started `runbolt` for each row, seeded 1,000 users, and measured each route with bombardier.
It read the server CPU time from `/proc`.
GDB captured the native stacks with `thread apply all bt`.
A sampler thread inside the server recorded `sys._current_frames()` every 20 ms for the Python stacks.
The driver, the sampler, and the raw output are not retained in the repository.

# Zero-copy response cleanup

Measured on 2026-09-13, after [the root investigation](PROFILE-FT-ROOT.md).

Keep `Bytes::from_owner`, but wrap `PyBackedBytes` in an owner with explicit cleanup.
Compile this owner only for free-threaded Python builds.
Regular builds use `PyBackedBytes` directly, without an additional attachment.
The wrapper calls `Python::try_attach` when the last Rust reference expires.
It releases the Python reference inside that attachment.
The response body never enters PyO3's deferred-reference queue during normal operation.

This changes ownership cleanup, not JSON encoding.
Every request still calls the root handler and msgspec.
The response uses the original Python buffer, with no payload copy or response cache.

The implementation uses safe Rust and PyO3's attachment API.
If attachment fails during shutdown, normal PyO3 field cleanup remains available.
Other Python objects can still activate PyO3's global queue.
This change does not remove that queue from PyO3.

The implementation is in [response_body.rs](../../src/response_body.rs).
Both bare JSON bytes and parsed wire responses use this owner.
Parsed owners also cover HEAD responses and response cancellation.

## HTTP comparison

All three branches used one optimized binary.
`ROOT_PROFILE_BODY_OWNER` selected `original`, `attach`, or `copy` once per process.
The copying branch was a diagnostic control.
Each configuration ran three times, in different orders.
Each measurement lasted six seconds, after a two-second warmup, with 100 connections.

| Configuration | Original owner | Attached owner | Copy control |
| --- | ---: | ---: | ---: |
| Three workers, median requests/sec | 186,461 | 203,980 | 200,358 |
| Three workers, median server CPU/request | 15.50 µs | 14.36 µs | 14.60 µs |
| Twelve workers, median requests/sec | 272,520 | 275,639 | 275,736 |
| Twelve workers, median server CPU/request | 22.67 µs | 21.99 µs | 21.72 µs |

The three-worker server used physical cores 0–2.
Its client used cores 3–5 and their SMT siblings, 9–11.
The twelve-worker comparison used the whole machine for both processes.

The attached owner improved median throughput by 9.4% and 1.1%, respectively.
Desktop applications remained active during these measurements.
Individual runs varied substantially, especially during the first twelve-worker comparison.
That first comparison measured 143k requests/sec for the original owner and 87k for the attached owner.
Later runs measured 272–273k and 276–279k, respectively.
These results do not establish a universal speedup of a fixed size.

## Native profile

Each branch ran for twenty seconds with twelve workers.
The sampler used a CPU timer on each worker, with a two-millisecond interval.
It did not stop worker threads.

| Native CPU samples | Original owner | Attached owner |
| --- | ---: | ---: |
| Total | 61,423 | 61,309 |
| `register_decref` | 119 | 0 |
| `drop_deferred_references` | 331 | 1 |
| `PyGILState` | 63 | 78 |

The response references no longer activate the deferred cleanup path.
These samples measure CPU activity, not time asleep on a mutex.
The extra attachment has a cost, but it does not restore the previous queue activity.

A release-build check measured 197,325 requests/sec before the change and 197,757 afterward on free-threaded Python.
Server CPU time fell from 21.07 to 20.58 microseconds per request.
Throughput was effectively unchanged in that run.
The cleanup profile is stronger evidence than a promise of a fixed throughput gain.

The first prototype also added cleanup attachments on regular Python.
A release check measured 196,017 requests/sec before that prototype and 188,344 afterward.
Those binaries had different compiler layouts, so this comparison cannot isolate attachment cost.
The final implementation avoids that additional work on regular builds entirely.

## Lifetime checks

Native tests check these properties:

- The response points to the original Python buffer.
- Clones and slices keep the buffer alive, including across Rust threads.
- The last owner releases Python storage before another request attaches to Python.

The final check fails without explicit cleanup and passes with it.
It also fails again when the cleanup implementation is removed.
Both tests pass on free-threaded Python 3.14.
Regular Python 3.12 runs the buffer-lifetime test with its original owner.

## Integration checks

Both installed extensions were rebuilt with the release profile.
The selected HTTP suite passed 114 tests on free-threaded Python and 113 on regular Python.
Two and three tests were skipped, respectively.
The suite covers response types, HEAD, serialization, compression, concurrent dispatch, and real TCP requests.
Library Ruff checks and `git diff --check` also passed.

The initial free-threaded load test stalled after exactly 10,947 successful requests.
The saved original extension failed at the same count.
Native stacks showed blocked log writes and logging locks.
The test harness retained output in pipes until shutdown.
Its debug access logs filled the pipe during the load test.
Draining those pipes resumed requests: 10,964 succeeded, with zero failures or retries.

The load test now configures `django.server` at WARNING.
Its request assertions remain unchanged.
This test configuration does not change production logging.

## Evidence

- [Benchmark runner](profiles/root-investigation/owner_suite.py)
- [Benchmark output](profiles/root-investigation/owner-suite.log)
- [Raw measurements](profiles/root-investigation/measurements.jsonl)
- [Failing lifetime test](profiles/root-investigation/owner-lifetime-red.log)
- [Passing lifetime tests](profiles/root-investigation/owner-lifetime-green.log)
- [Tests in the owning crate](profiles/root-investigation/owner-native-tests.log)
- [Original native profile](profiles/root-investigation/native-owner-original-summary.txt)
- [Attached native profile](profiles/root-investigation/native-owner-attach-summary.txt)
- [Free-threaded HTTP checks](profiles/root-investigation/owner-ft-http-final.log)
- [Regular Python HTTP checks](profiles/root-investigation/owner-gil-http-final.log)
- [Regular Python native check](profiles/root-investigation/owner-native-gil-tests.log)
- [Original extension's load failure](profiles/root-investigation/owner-ft-load-original.log)
- [Log pipe control](profiles/root-investigation/owner-log-pipe-probe.log)
- [Blocked logging stacks](profiles/root-investigation/owner-log-pipe-stacks.txt)

This fix concerns buffered HTTP responses.
The shared `JSON_10K` fixture has a separate msgspec container-lock bottleneck.
See [the JSON investigation](PROFILE-FT-JSON.md).

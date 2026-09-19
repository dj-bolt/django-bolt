# Root endpoint: response cleanup and dispatch profile

Measured on 2026-09-13. This investigation profiles `/`, which returns a new dictionary for every request.

Follow-up: [a zero-copy cleanup fix](PROFILE-FT-ZERO-COPY.md) now replaces the copying proposal below.

**A verified root-path bottleneck is PyO3's global deferred-reference queue, activated by Bolt's response ownership.**
The response payload is private, but its cleanup enters shared state.
This is separate from contention on the shared `JSON_10K` list.

The request follows this path:

1. The handler creates `{"message": "Hello Django"}`.
2. msgspec encodes it into 26 Python-owned bytes.
3. Bolt extracts `PyBackedBytes` and passes it to `Bytes::from_owner`.
4. Rust retains the Python reference while it sends the response.
5. Rust releases the body after leaving `Python::attach`.
6. `Py<T>::drop` queues the reference in PyO3's process-wide `ReferencePool`.
7. Later Python attachments lock and drain that queue.

All workers in one process share this queue.
Separate processes have separate queues.
The queue also transfers reference cleanup between threads.
For this tiny response, retaining Python ownership saves a 26-byte copy but adds deferred cleanup and synchronization.

The relevant code is:

- [Response ownership](../../src/handler.rs#L1288): `PyBackedBytes` and `Bytes::from_owner`.
- [Python dispatch](../django_bolt/api.py#L2824): metadata lookup and `request.get("auth")`.
- [PyO3 reference release](/home/farhan/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/pyo3-0.29.2/src/instance.rs:2284).
- [PyO3 queue and mutex](/home/farhan/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/pyo3-0.29.2/src/internal/state.rs:191).

Native stacks captured this wait on root requests:

```text
handle_request
  Python::attach
    AttachGuard::do_attach_unchecked
      ReferencePool::drop_deferred_references
        Mutex::lock_contended
          futex_wait
```

Another entry came from `request.get("auth")`:

```text
PyRequest.get trampoline
  AttachGuard::assume
    ReferencePool::drop_deferred_references
      Mutex::lock
```

The root route has no authentication, but its dispatcher still calls that Rust-backed method.
PyO3 drains deferred references even for this nested entry into an attached Python thread.

The ownership experiment changed only the body construction in an isolated source copy.
It used `Bytes::copy_from_slice(&backed)` while Python remained attached.
The Python byte references then expired before leaving the attached scope.
The root handler, JSON encoding, response contents, and response headers stayed the same.
There was no response cache.

Both branches exist in one compiled extension, selected once by `ROOT_PROFILE_COPY_BODY`.
This avoids comparing different compiler layouts for the ownership A/B test.
The [exact patch](profiles/root-investigation/copy-body.patch) contains the complete change.

The native evidence changed as follows:

| Measurement | Python-owned response body | Copied response body |
| --- | ---: | ---: |
| GDB worker snapshots inside queue draining | 17 / 288 | 0 / 144 |
| GDB worker snapshots in contended mutex waits | 15 / 288 | 0 / 144 |
| Native CPU samples in `register_decref` | 79 / 38,236 | 0 / 38,021 |
| Native CPU samples in `drop_deferred_references` | 216 / 38,236 | 2 / 38,021 |

GDB observations establish the call path; they are not percentages of CPU time.
Stopping threads can change contention, so a second sampler ran without stopping workers.
It used a CPU timer for each worker, with a two-millisecond interval and thread-targeted signals.
It recorded instruction addresses and resolved them through ELF load segments and debug symbols.
CPU samples do not measure time spent asleep on a mutex.

Unprofiled HTTP runs measured the ownership change separately.
The server used physical cores 0–2; the client used cores 3–5 and their SMT siblings 9–11.
Both branches used three FT workers, concurrency 100, and four-second measurement windows after warmup.

| Controlled series | Original body | Copied body | Original CPU time/request | Copied CPU time/request |
| --- | ---: | ---: | ---: | ---: |
| Interleaved runs, median | 206,981 req/s | 219,307 req/s | 14.37 µs | 13.54 µs |
| Later repeat, median | 214,432 req/s | 217,202 req/s | 13.65 µs | 13.22 µs |

The ownership change removed the observed queue activity and reduced server CPU time by 0.4–0.8 microseconds per request.
Throughput improved by about 1–6% across these series.
The machine also ran desktop applications, so small throughput differences varied between runs.
These figures do not promise a fixed percentage gain in production.

The Python dispatcher has another measurable cost on this minimal endpoint.
A separate diagnostic bound the existing compiled executor directly for the root route.
It kept the original handler and JSON encoding but bypassed the metadata lookup, auth check, and dispatcher wrapper.
With the original body ownership, throughput rose from 214,432 to 237,880 requests per second in that series.
Combining both diagnostic changes reached 246,207 requests per second.
This measures removable work on the successful root path; it does not prove a second FT-specific regression.
The diagnostic bypass does not preserve the dispatcher's full error behavior and is not a production replacement.

The original reports compare Python 3.12, eight processes, and one worker against Python 3.14 FT with twelve workers.
The installed binaries reproduced a smaller root gap: 310,935 FT versus 336,804 requests per second with Python 3.14 GIL.
With matched profiling builds and three server cores, medians were 214,432 FT and 213,387 GIL.
Those overlapping results do not establish an inherent FT deficit for this configuration.
Single-worker CPU profiles measured 71,303 FT and 73,719 GIL requests per second, with instrumentation enabled.
These profiles showed similar broad CPU distributions and no large msgspec encoding hotspot.

Therefore, the shared cleanup path is a demonstrated cost, but it does not justify attributing the original report's entire gap to one lock.
Worker topology, build differences, and run variation must remain separate from the ownership A/B result.

A production fix can avoid Python-backed ownership for sufficiently small response bodies.
The appropriate size threshold needs measurements across response sizes.
A general change must also cover response tuples and preserve streaming and lifetime behavior.
Prebinding root dispatch work is a separate optimization that must preserve exception handling and route features.

This initial investigation left the normal extension, production source, and original benchmark reports unchanged.
Only its isolated profiling source used the ownership switch.
It completed 73 HTTP measurement runs and 67,574,913 successful responses, with no reported HTTP errors.
The retained Python profiling tools pass Ruff.

Evidence is retained inside the repository, under [root-investigation](profiles/root-investigation/):

- [Raw HTTP measurements](profiles/root-investigation/measurements.jsonl)
- [Extracted queue stacks](profiles/root-investigation/pool-stacks.txt)
- [Original CPU profile summary](profiles/root-investigation/native-original-summary.txt)
- [Copied-body CPU profile summary](profiles/root-investigation/native-copy-summary.txt)
- [Benchmark driver](profiles/root-investigation/driver.py)
- [Isolated server launcher](profiles/root-investigation/server.py)
- [Native sampler](profiles/root-investigation/native_sampler.c)
- [Symbol resolver](profiles/root-investigation/analyze_native.py)

The generated environments, compiled extensions, source copy, and wheels are locally ignored.
The source patch and profiling scripts provide the experiment's implementation.

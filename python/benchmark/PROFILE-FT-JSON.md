# Free-threaded JSON profile

Measured on 2026-09-13, using the installed extensions in this checkout.

The large JSON slowdown comes from contention on the shared response list.
All worker threads encode the same `test_data.JSON_10K` object.
msgspec locks that list during encoding, so workers wait for each other.
Each process has its own Python objects and locks, which avoids this contention between processes.

Both routes return the same fixture:

- [Async handler](../example/testproject/api.py#L475)
- [Sync handler](../example/testproject/api.py#L503)
- [Fixture loader](../example/test_data/__init__.py)

The fixture contains 10 records. Its encoded response is 10,864 bytes.
The handler docstrings incorrectly describe it as 10k objects.

msgspec 0.21.1 wraps list traversal in `Py_BEGIN_CRITICAL_SECTION(obj)`.
It also uses critical sections for dictionaries.
The encoder method creates its encoding state for each call; the shared encoder is not the measured bottleneck.
See [the matching msgspec source](https://github.com/msgspec/msgspec/blob/0.21.1/src/msgspec/_core.c#L14060).

Native stack snapshots confirm the mechanism under HTTP load:

```text
JSONEncoder_encode
  json_encode_list
    _PyCriticalSection_BeginSlow
      _PyMutex_LockTimed
        semaphore / futex wait
```

Five GDB snapshots captured all 12 Actix workers during `/sync-10k-json` load.
The shared fixture produced 52 waiting worker stacks out of 60.
The counts per snapshot were 10, 11, 9, 11, and 11.
With a private fixture for each thread, the corresponding counts were all zero.
These are stack observations, not percentages of CPU time.

The installed py-spy 0.4.2 failed with `failed to get gil_thread_id` on this free-threaded interpreter.
The kernel blocked perf recording with `perf_event_paranoid=4`.
GDB provided native stack attribution instead.
Throughput measurements ran separately, without a debugger attached.

The direct encoding experiment changed payload ownership and encoder ownership independently.
Each private payload was decoded once inside its worker thread, before timing.
No experiment copied the payload for each encode operation.
The table shows medians of three runs, each lasting approximately one second.

| Python | Threads | Payload | Encoder | Encodes/s | CPU cores used |
| --- | ---: | --- | --- | ---: | ---: |
| 3.12.13 GIL | 1 | Shared | Shared | 136,859 | 1.00 |
| 3.12.13 GIL | 12 | Shared | Shared | 117,201 | 1.01 |
| 3.14.6 GIL | 1 | Shared | Shared | 132,384 | 1.00 |
| 3.14.6 GIL | 12 | Shared | Shared | 114,892 | 1.02 |
| 3.14.6 FT | 1 | Shared | Shared | 106,077 | 1.00 |
| 3.14.6 FT | 4 | Shared | Shared | 78,840 | 1.26 |
| 3.14.6 FT | 4 | Private | Shared | 437,989 | 3.99 |
| 3.14.6 FT | 12 | Shared | Shared | 71,761 | 1.24 |
| 3.14.6 FT | 12 | Shared | Private | 72,324 | 1.24 |
| 3.14.6 FT | 12 | Private | Shared | 695,569 | 11.15 |
| 3.14.6 FT | 12 | Private | Private | 701,160 | 11.30 |

Private encoders do not remove the slowdown.
Private payloads increase encoding throughput by about 9.7 times with the existing shared encoder.
The shared fixture uses roughly one CPU core because the other threads wait.
Additional threads make this encoding workload slower through contention and thread scheduling.
The Python 3.14.6 GIL control used the same msgspec version in a later run.
Its single-thread result was about 25% higher than FT, showing some remaining interpreter overhead.
That difference is much smaller than the shared-payload contention effect.

The HTTP experiment used the existing example application and handlers.
A temporary launcher replaced fixture access or the encoder before route registration.
The private fixture variant used `threading.local` and decoded the JSON once per worker.
It retained the existing shared encoder and performed JSON encoding on every request.

Each endpoint had a one-second warmup followed by three two-second runs at concurrency 100.
The table shows median requests per second.

| Runtime and configuration | Fixture / encoder | Root | Async JSON | Sync JSON |
| --- | --- | ---: | ---: | ---: |
| FT, 1 process × 12 workers | Shared / shared | 293,245 | 48,379 | 48,587 |
| FT, 1 process × 12 workers | Shared / private | 279,891 | 47,859 | 48,289 |
| FT, 1 process × 12 workers | Private / shared | 283,747 | 157,756 | 169,976 |
| FT, 1 process × 4 workers | Shared / shared | 199,667 | 52,447 | 54,622 |
| FT, 1 process × 4 workers | Private / shared | 201,863 | 105,516 | 110,695 |
| FT, 1 process × 1 worker | Shared / shared | 78,268 | 34,625 | 33,767 |
| GIL, 8 processes × 1 worker | Shared within each process | 319,375 | 176,029 | 182,922 |

Private payloads improve HTTP throughput by 3.3 times for async and 3.5 times for sync.
The original shared fixture reproduces the reported 47–49k requests per second.
The process comparison gives each process a separate fixture, while the thread comparison shares one fixture.
This difference strongly affects what the benchmark measures.

A separate control cached the bytes and returned `Response(..., media_type="application/json")`.
It reached 217,120 async and 231,649 sync requests per second with 12 FT workers.
The control checked the JSON content type and parsed each endpoint's response before load.
This control serves cached bytes and does not measure JSON encoding.
All 90 HTTP measurement runs reported zero error responses.

The machine has a Ryzen 5 5600 with 6 physical cores and 12 hardware threads.
Both environments use msgspec 0.21.1 and Django 6.1.
The GIL environment uses Python 3.12.13; the FT environment uses Python 3.14.6 with the GIL disabled.
The existing reports therefore change both Python version and process topology.
The server and load generator shared the machine without CPU affinity.
Small differences need longer runs with controlled CPU allocation.
The large contention effect is supported by the independent encoding experiment and native stacks.

For a serialization benchmark, give each worker its own fixture and continue encoding each response.
Keep a separate shared-fixture test if contention on shared application data matters.
For a constant production response, cache the encoded bytes and return an explicit JSON response.
Pre-encoding changes the benchmark into a cached-response test.
Do not replace the shared encoder to address this result; that change did not help.
Do not add automatic deep copies to the general response path based on this fixture.

The `/feed` handler constructs a new list and new objects for every request.
Its smaller reported FT difference is consistent with avoiding the shared fixture.
This investigation did not profile ORM endpoints; the JSON result does not establish their bottleneck.

Reproduce the direct encoding comparison from the repository root:

```bash
.venv-ft/bin/python python/benchmark/profile_json_threads.py --threads 12 --mode shared --seconds 3
.venv-ft/bin/python python/benchmark/profile_json_threads.py --threads 12 --mode local-encoder --seconds 3
.venv-ft/bin/python python/benchmark/profile_json_threads.py --threads 12 --mode local-data --seconds 3
.venv-ft/bin/python python/benchmark/profile_json_threads.py --threads 12 --mode local-both --seconds 3
```

The [probe script](profile_json_threads.py) also accepts `--threads 1` and `--threads 4`.
Use `.venv/bin/python` for the installed GIL environment.

Raw evidence and the exact temporary HTTP launchers are retained in `/tmp/bolt-ft-profile`:

- Encoding measurements: `/tmp/bolt-ft-profile/micro-results.jsonl`
- HTTP measurements: `/tmp/bolt-ft-profile/http-results.jsonl`
- Python 3.14 GIL control: `/tmp/bolt-ft-profile/same-version-results.jsonl`
- Cached JSON response control: `/tmp/bolt-ft-profile/http-json-bytes-results.jsonl`
- Shared-fixture stack snapshot: `/tmp/bolt-ft-profile/ft-1p-12w-baseline-gdb-0.txt`
- Private-fixture stack snapshot: `/tmp/bolt-ft-profile/ft-1p-12w-local-data-gdb-0.txt`
- HTTP benchmark driver: `/tmp/bolt-ft-profile/run_http.py`
- Temporary server launcher: `/tmp/bolt-ft-profile/server.py`

Production handlers, framework code, and the original benchmark reports were not changed by this investigation.

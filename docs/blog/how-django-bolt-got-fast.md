# How I Made (Probably) the Fastest Python Web Framework

*The story of Django-Bolt: from a weekend experiment at 19k requests/sec to 311k requests/sec — a 16x improvement on the same machine, over ten months of commits.*

Every number in this post comes from benchmark files committed to the repo at the time, all measured on the same desktop: a Ryzen 5 5600G with 16 GB of RAM. Nothing here is a cloud benchmark or a synthetic best case — it's the same `bombardier`/`ab` runs against the same endpoints, commit after commit.

## It started as an experiment

The first commit, September 19, 2025, is literally named `batman`. I didn't know if the idea would work: put Actix Web (Rust) in front of Python, route requests in Rust with zero-copy path matching, and only cross into Python to run the actual handler — inside a real Django project, with the real Django ORM.

That first commit was already the whole bet: PyO3 + Actix + Tokio + matchit, a 221-line `lib.rs`, and a plan.md that laid out everything that exists today — auth in Rust, middleware in Rust, multi-process SO_REUSEPORT scaling. The baseline it recorded: **~19.5k RPS** for hello-world JSON through a Python handler. Django itself managed ~2k RPS on the ORM endpoint.

The README from that week says it plainly:

> "This is an experimental project. The API is unstable and may change without notice. **Do not use in production yet.**"

The next day: "27k helloworld." A day after that, Django fully wired in: **~43k RPS**, next to FastAPI's 3.8k on the same box.

### The first real discovery: fewer threads, more processes

The obvious config — 2 processes × 2 Actix workers — did 43k. The counterintuitive one — **4 processes × 1 worker each** — did **~68k RPS**, and doubled ORM throughput. Multiple worker threads inside one process just fight over the GIL. One interpreter, one GIL, one worker thread per process, and let the kernel load-balance across processes with SO_REUSEPORT. That decision never changed again.

**Where we stood: ~68k RPS.**

## Streaming almost broke me

I spent a huge part of the early months on streaming, and the history shows why.

The first streaming implementation (Sep 29) had *two* competing batching layers — a Python-side "async collector" that gathered chunks into batches, and a Rust-side loop that pulled batches of `__anext__()` futures — both trying to amortize GIL crossings. It also taught me my first measurement lesson: `ab` reported SSE at ~1,200 RPS while `hey` reported the *same endpoint* at ~26,700. The bottleneck was the benchmark tool, not the server.

Then it got worse. Under real concurrent load, **the server stopped accepting SSE connections after ~200 client disconnects.** The cause: when a client disconnected, the sync generator's thread only broke out of the inner batch loop — the outer loop kept calling `__next__()` forever. Each zombie thread permanently ate one slot in Tokio's bounded blocking pool until nothing was left. Python `finally` blocks never ran either, so DB connections leaked too.

The fix (Nov 7, PR #25) was to delete cleverness, not add it:

- **Removed batching entirely.** Both layers. SSE sends every chunk immediately.
- Sync generators moved to dedicated OS threads with a hard cap, off the shared pool.
- Generators get properly `.close()`d / `.aclose()`d on disconnect.

After the rewrite: 10,000 concurrent SSE clients, 100% success rate, ~9,500 messages/sec, at 11.9% average CPU. The lesson stuck: batching was optimizing the wrong thing. The real cost was never the per-chunk GIL crossing — it was architectural (which thread, which loop, who cleans up).

## The GIL ledger

October 2025 was about one question: how many times per request do we acquire the GIL, and what happens while we hold it?

- **Batch GIL** (Oct 21, #12): CORS and rate limiting moved fully into Rust — config parsed once at startup into Rust structs, headers pre-computed. GIL acquisitions per request: **6 → 3**.
- **Event loop reuse + off-GIL body copy** (Oct 22, #13): resolve the asyncio loop once at startup instead of per request, and copy the response body *after* releasing the GIL (grab pointer + length under the GIL, memcpy outside it). Mid-refactor throughput of 32k jumped to **66.6k** at 8 processes.
- **Cached msgspec encoders** (Oct 22): 72k.
- **uvloop + Rust-side compression** (Oct 31): **101k RPS.** First time past 100k.

**Where we stood: ~101–106k RPS**, where it plateaued for two months.

That plateau taught the framework its core principle, later written into the contributor docs as a rule: **do it once at registration, reuse forever at runtime.** November and December were spent compiling things ahead of time — per-handler argument injectors compiled to closures at route registration, AST analysis of handler source to detect blocking ORM calls (so pure-CPU sync handlers skip the thread pool entirely), pre-computed `needs_body`/`needs_headers` flags so Rust doesn't parse what the handler won't read. None of it moved the hello-world number much. All of it was load-bearing for what came next.

Also in this window: the Django ORM path got its biggest early win. Draining a QuerySet with `async for` secretly does a `sync_to_async` hop *per 100-row chunk*. Replacing that with one `sync_to_async(list)` call plus a single batched `msgspec.convert()` turned ~100 thread hops into one.

## Moving the request into Rust

January 9, 2026 (#78): type coercion left Python. Rust now parses ints, UUIDs, Decimals, and datetimes from path/query/header/cookie strings and builds *already-typed Python objects* directly. Invalid params get their 422 response from Rust before Python is ever invoked. Multipart form parsing moved to Rust too. **~120k RPS**, and — more importantly — the p99 latency shape improved, because the Python hot path stopped re-parsing strings.

February continued the pattern: cookie serialization moved to Rust (Python passes raw tuples, Rust builds `Set-Cookie`), and **Rust-side argument prebinding** landed — for simple handlers, Rust constructs the handler's `args`/`kwargs` directly from its own parsed maps and skips the Python injector completely.

**Where we stood: ~117k RPS.** Solid, incremental… and then the biggest jump of the project so far.

## Killing the async bridge, part one

March 2026, PRs #156/#164. The insight: for a huge class of handlers, the async machinery was pure overhead. Crossing from Rust into asyncio — creating a coroutine, wrapping it in a Task, polling the loop — cost ~6–12μs per request *even when the handler never actually awaited anything*.

So the framework got a **sync dispatch bypass**: if a route has no middleware and no signals, Rust calls a plain Python function, parses the response, and builds the HTTP response inside a single GIL block. No coroutine, no Task, no loop.

The trick that makes this widely applicable: **trivially-async detection.** At registration, `dis.get_instructions()` scans the handler's bytecode for the `GET_AWAITABLE` opcode. An `async def` that never awaits can be driven synchronously — `coro.send(None)` raises `StopIteration` with the result. Your handler stays `async def`; the async bridge disappears.

Alongside it: integer meta tags (common response types map to static Rust constants — zero allocation), inline dict/list serialization, and zero-copy response bodies via `PyBackedBytes` → `Bytes::from_owner()` — no memcpy of the serialized body at all (a pattern I found reading Granian's source).

The result was the biggest single leap yet: root endpoint **117k → 171–180k RPS (+50%)**, path+query **~107k → ~149k**, 10kb JSON **~90k → ~124k**. The README headline changed from "60k+ RPS" to "188k+ RPS."

**Where we stood: ~188k RPS.**

## An honest detour: the optimization PR that deleted itself

Worth telling because it's the opposite of a victory lap. In July I did a RAM-consumption pass (#232): pre-allocated buffers, a 1-byte method enum, a hashmap route store, a middleware bypass. Then I A/B soak-tested them — and **almost all of them were noise** (~157 MiB RSS either way). The final commit reverted most of the PR, keeping only the two changes that measurably mattered: payload-size enforcement (the body-read loop had been unbounded — a real OOM vector) and a bounded logging queue (+3 MiB under a 500k-record log flood, versus +166 MiB unbounded).

If you don't measure, you're just collecting plausible-sounding code.

## Killing the async bridge, part two: the WorkerLoop

July 2026, #268 — the big one. This time it started with profiling rather than intuition: py-spy with `--gil` plus strace against a live server. Two findings changed everything:

1. **asyncio's Task machinery was ~50% of all GIL time under async load.** The `ensure_future` path — wrapping every handler coroutine in a Task and hopping to a separate loop thread — added ~45% latency to a trivial async request. The actual suspend/resume of a real `await`? Nearly free. We weren't paying for async; we were paying for the *bridge*.
2. **strace showed a 16 KiB mmap/munmap pair on every request** — PyO3 creating and destroying a Python thread state each time Rust touched Python. Pinning thread states per worker thread cut syscalls from 57.5k to 32.7k per 8k requests, worth ~15% on sync dispatch by itself.

The fix for (1) is the **WorkerLoop**: a process-lived asyncio-loop facade whose ready queue is serviced by a persistent Tokio pump. Handler coroutines start eagerly — the first segment runs synchronously, and only a real suspension promotes to a Task. Awaits that complete immediately never touch the loop machinery at all. Short timers get a dedicated GIL-free Rust timer thread, because Tokio's ~1ms timer wheel was measurably delaying short `asyncio.sleep()` calls.

The same PR held a genuinely counterintuitive ORM result. Evaluating QuerySets on an unbounded thread pool — the "obviously better" parallel approach — **collapsed SQLite throughput by 52%**, because many connections contend on SQLite's file lock. The sweep was unambiguous at 32 concurrent clients: 1 thread → 1,329 RPS; 2 → 1,044; 4 → 863; unbounded → 727. So the ORM executor is bounded and vendor-aware: SQLite gets one thread, everything else gets a small pool. Sometimes the fastest concurrency is almost none.

The measured jump, one commit: **198.7k → 338k RPS (+71%).** ORM endpoints roughly doubled against the old sequential path.

One week later came the last architectural fix: streaming and WebSockets were still scheduled on the *old* startup loop while HTTP dispatch had moved to the WorkerLoop — and an asyncio `Future` resolved on one loop never wakes a waiter on another. Ten months in, streaming was still teaching the same lesson: loop identity matters more than loop speed.

## Where we are

Current numbers (8 processes, C=100, N=100k, same Ryzen 5 5600G):

| Endpoint | Reqs/sec | p99 |
|---|---|---|
| Root JSON | **311k** | 2.2ms |
| Path + header/cookie params | ~255k | 1.9ms |
| msgspec Struct response | ~288k | 2.0ms |
| JSON parse + validate | ~251k | 2.0ms |
| 10kb JSON | ~184k | 1.8ms |
| JWT auth (no DB) | ~158k | 1.6ms |
| Form data | ~218k | 2.4ms |
| File upload | ~178k | 2.0ms |
| ORM, 10 rows (async, SQLite) | ~21k | 8.1ms |
| Full Django middleware + template | ~8.7k | 21ms |

The journey, in one table:

| When | What changed | Root RPS |
|---|---|---|
| Sep 2025 | First commit ("batman") | ~19.5k |
| Sep 2025 | 4 processes × 1 worker, SO_REUSEPORT | ~68k |
| Oct 2025 | GIL batching, loop reuse, off-GIL copies, uvloop | ~101k |
| Jan 2026 | Type coercion + form parsing → Rust | ~120k |
| Mar 2026 | Sync dispatch bypass, trivially-async, zero-copy bodies | ~188k |
| Jul 2026 | WorkerLoop, thread-state pinning, bounded ORM executors | **~311k** |

**16x, same hardware.** And the last two rows of the endpoint table are the honest part: once a request touches the ORM or the full Django middleware stack, you're bound by Django and the database, not the framework. The framework's job is to make everything *around* your actual work cost as close to zero as possible.

## What I'd tell you if you're trying this

1. **Count your GIL acquisitions.** Not "is the GIL a problem" — literally count them per request, and what you do while holding them. We went 6 → 3 → 1.
2. **The async bridge is the tax, not async itself.** Coroutine + Task + loop-wakeup machinery dwarfed actual suspension cost. Twice — in March and again in July — the biggest wins came from not entering asyncio at all.
3. **Do it once at registration.** Every isinstance chain, every `dict.get`, every string-dispatch branch on the hot path can usually be compiled into a closure or a flag when the route is registered.
4. **Profile the boundary, not just the code.** py-spy `--gil` and strace found the two biggest wins (Task overhead, per-request mmap churn). Neither shows up in a Python profiler.
5. **Measure, and let the measurement win.** The benchmark tool lied about SSE. The "obvious" parallel ORM pool was 2.5x slower on SQLite. Half a RAM-optimization PR got reverted because A/B soak tests said it was noise.
6. **Delete cleverness that fights the architecture.** Streaming got fast when batching was removed, not tuned.

It started as an experiment I wasn't sure would work. 451 commits later, a Django app serves JSON at 311k requests/sec on a desktop CPU — with the Django ORM, Django admin, and Django middleware still a decorator away.

*Django-Bolt is open source: [github.com/dj-bolt/django-bolt](https://github.com/dj-bolt/django-bolt).*

# CLAUDE.md

Django-Bolt: Rust-powered (Actix Web + PyO3) API endpoints for Django, msgspec serialization, multi-process via SO_REUSEPORT. See [docs/README.md](docs/README.md) for usage docs and [docs/PROFILING.md](docs/PROFILING.md) for measurement.

## Commands

```bash
just build                 # rebuild the Rust extension (required after any Rust change)
just rebuild               # clean + build
just test-py               # Python tests
uv run --with pytest pytest python/tests/test_syntax.py::test_name -s -vv
just lint                  # ruff on everything; `just lint-lib` must always pass
just format
just save-bench            # benchmarks → python/benchmark/BENCHMARK.md (needs bombardier)
python manage.py runbolt --dev              # from python/example
just release VERSION=0.2.2 [DRY_RUN=1]      # bump, commit, tag, push
```

Supported: Python 3.12–3.14 (source of truth: `pyproject.toml` classifiers), Django 4.2–6.1.

## Layout

Cargo workspace, layered bottom-up with no back-edges: `bolt-loop` → `bolt-core` → {`bolt-asgi`, `bolt-websocket`, `bolt-mcp`} → root `django-bolt` (`src/`, the `_core` PyO3 module).

- `src/` — `lib.rs` (module entry), `server.rs` (Actix + tokio, CORS/compression), `handler.rs` (dispatch, `DispatchOutcome`), `testing.rs` (`TestClient` backend), `dev_reload.rs`
- `crates/bolt-loop` — process-lived asyncio `WorkerLoop` (a real `SelectorEventLoop` on the Tokio reactor), timer thread
- `crates/bolt-core` — router (matchit), `request_pipeline.rs`, `validation.rs`, `type_coercion.rs`, `middleware/` (auth, rate_limit), `permissions.rs`, `streaming.rs`, `response_meta.rs` / `response_builder.rs`, `form_parsing.rs`, `metadata.rs`, `error.rs`
- `crates/bolt-asgi`, `crates/bolt-websocket`, `crates/bolt-mcp` — ASGI mounts, WebSocket, MCP transport (rmcp)
- `python/django_bolt/` — `api.py` (BoltAPI, decorators, `_dispatch`/`_dispatch_sync`), `_kwargs/` (param extraction/injectors), `serialization.py` (ResponseWireV1, meta tags), `responses.py`, `params.py`, `dependencies.py`, `auth/`, `middleware/` (`compiler.py` → Rust metadata), `serializers/`, `openapi/`, `pagination.py`, `viewsets.py`, `concurrency.py`, `workers.py`, `management/commands/runbolt.py`, `testing/`

## Core design (keep these invariants)

- **Dual dispatch** (`src/handler.rs`): sync path (`dispatch_sync.call1()` in one GIL block, `DispatchOutcome::Ready`) when `can_sync_dispatch`; `SyncResult` for sync handlers returning stream/file; async path (`Pending`) submits to the `WorkerLoop`. Trivially-async handlers (no `GET_AWAITABLE` in bytecode) get a `_sync_executor`.
- **ResponseWireV1**: Python returns `(status, meta, body_kind, body)`; meta is an int tag (0=JSON,1=text,2=octet,3=empty → static Rust `ResponseMeta`) or a `(response_type, custom_ct, headers, cookies)` tuple; body_kind 0=bytes (zero-copy `PyBackedBytes`), 1=stream, 2=file. Sync routes may return bare `bytes` for (default status, JSON). New common response type ⇒ add both the Python constant and the Rust static.
- **Cookies are serialized in Rust** from raw 9-tuples; `MiddlewareResponse` never serializes them.
- **Executors**: QuerySet-returning async handlers evaluate on the bounded ORM pool (`concurrency.run_in_orm_executor`, `DJANGO_BOLT_ORM_THREADS`); generic `sync_to_thread` shares one bounded default pool (`DJANGO_BOLT_EXECUTOR_THREADS`). Do not add another implicit pool. Avoid `async for`/`acount()` on hot paths; return the QuerySet.
- **WorkerLoop** callbacks may resume on a different OS thread (contextvars survive, `threading.local` does not). Keep the pump fast (`call_soon` ≈ 0.75–0.85x uvloop).
- Auth, guards, CORS, rate limiting, compression run in Rust without the GIL; middleware config is compiled to Rust metadata at startup.

## Hot-path rules

Do it once at registration, reuse forever at runtime. In `_dispatch`, `_dispatch_sync`, `serialize_response`, `_kwargs/` injectors, `dependencies.py`:

- Route meta keys (`mode`, `is_async`, `default_status_code`, `response_type`) are guaranteed at registration; use `meta["key"]`, never `meta.get(k, default)`.
- No string dispatch in loops (pre-sort into source buckets), no per-request temp dicts/lists, no `hasattr`, no `isinstance` after a guaranteed conversion, no double dict lookups, no per-response tuple construction for common types.
- One `msgspec.convert()` over field-by-field validation; module-level singleton encoders; `__slots__` on request/state objects; `functools.partial` to pre-bind per-route config.
- Extract data from Python at registration and evaluate natively in Rust — never per-request Python callbacks.

## Standards

- Smallest surgical change for the requested scope; delete obsolete machinery rather than adding compatibility state.
- Never silently ignore errors. Imports at module top. `from __future__ import annotations`.
- Library code (`python/django_bolt/`) must pass ruff. Some S110 in test WebSocket handlers is acceptable.
- Never add Co-Authored-By / AI trailers to commits or PRs.
- **Writing style**: docs (`docs/`, README), code comments and docstrings, and `CHANGELOG.md` entries use ASD-STE100 Simplified Technical English: short sentences (max 20 words per instruction, 25 per description), one instruction per sentence, active voice, simple tenses, one meaning per word, no noun clusters over three nouns, start instructions with a verb. Code identifiers stay as they are.

## Testing

- **Red-Green**: write the test first, see it fail, implement, confirm it fails again when reverted. A test that passes without the change is bogus. Never delete failing asserts or skip tests to go green.
- Use `TestClient(api)` (in-process, full Rust pipeline, lifespan-aware) for integration tests. Test behavior via HTTP responses, not internals; no mocks of things we own.
- Subprocess `runbolt` tests only for what `TestClient` cannot exercise: startup wiring, `--dev` reload, multi-process, signals, real TCP, streaming, WebSocket handshakes, artifacts. Author such apps as real modules in `python/tests/integration/apps/` (self-contained `api = BoltAPI()` + `/health`; secondary apps use a namespaced health path). Prefer `make_server_project(api_module=app_module("x"))`; use `api_source=app_source("x")` only when an on-disk file is needed (autodiscovery, reload, artifact tests).
- Markers: `server_integration` (real `runbolt`), `platform_smoke`, `artifact_smoke`. Apply `server_integration` per subprocess test, never module-wide when the module also has in-process tests. Changes to startup/reload/multiprocessing/TCP/streaming/WebSocket/packaging need a `server_integration` or `artifact_smoke` test.
- Rust tests: `#[cfg(test)]` next to the code in the owning crate.
- Run with `-s -vv`. bolt-mcp shares the integration harness — when changing `helpers.py`, grep repo-wide under `python/` and run the bolt-mcp suite too.

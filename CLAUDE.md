# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Date

Now is December 23, 2025

## Project Overview

Django-Bolt is a high-performance API framework for Django that provides Rust-powered API endpoints with 60k+ RPS performance. It integrates with existing Django projects, using Actix Web for HTTP handling, PyO3 to bridge Python handlers with Rust's async runtime, msgspec for fast serialization, and supports multi-process scaling with SO_REUSEPORT.

## Key Commands

### Build & Development

```bash
# Build Rust extension (required after any Rust code changes)
just build

# Full rebuild (clean + build)
just rebuild

# Clean build artifacts
just clean
```

### Running the Server

```bash
# From Django project directory (e.g., python/example)
python manage.py runbolt --host 0.0.0.0 --port 8000 --processes 1

# Development mode with auto-reload (single process, watches for file changes)
python manage.py runbolt --dev

# Background multi-process (for testing)
just run-bg HOST=127.0.0.1 PORT=8000 P=2

# Kill any running servers
just kill
```

### Testing

```bash
# Python unit tests
just test-py

# Run specific test file
uv run --with pytest pytest python/tests/test_syntax.py -s -vv

# Run specific test function
uv run --with pytest pytest python/tests/test_syntax.py::test_streaming_async_mixed_types -s -vv

# Quick endpoint smoke tests
just smoke      # Test basic endpoints
just orm-smoke  # Test ORM endpoints (requires seeded data)
```

### Code Quality & Linting

```bash
# Run ruff linter on all code (library, tests, examples)
just lint       # or: just ruff

# Check only library code (excludes tests and examples)
just lint-lib   # Should always pass - library code must be clean

# Fix auto-fixable errors
just ruff-fix

# Format code with ruff
just format
```

**Note**: Some S110 errors (try-except-pass) in test/example files are acceptable for WebSocket handlers where client disconnection is expected. All library code (`python/django_bolt/`) must pass all linting checks.

### Benchmarking

```bash
# Full benchmark suite (saves results)
just save-bench  # Creates/rotates BENCHMARK_BASELINE.md and BENCHMARK_DEV.md

# Custom benchmark
just bench C=100 N=50000  # 100 concurrent, 50k requests

# High-performance test
just perf-test  # 4 processes × 1 worker, 50k requests

# ORM-specific benchmark
just orm-test   # Sets up DB, seeds data, benchmarks ORM endpoints
```

### Database (Standard Django)

```bash
# From Django project directory
python manage.py migrate
python manage.py makemigrations [app_name]
```

### Release

```bash
# Create a new release (bumps version, commits, tags, and pushes)
just release VERSION=0.2.2              # Standard release
just release VERSION=0.3.0-alpha1       # Pre-release
just release VERSION=0.2.2 DRY_RUN=1    # Test without changes

# Or use the script directly
./scripts/release.sh 0.2.2              # Standard release
./scripts/release.sh 0.2.2 --dry-run    # Test without changes
```

## Architecture Overview

### Core Components

1. **Rust Layer (`src/`)**

   - `lib.rs` - PyO3 module entry point, registers Python-callable functions
   - `server.rs` - Actix Web server with tokio runtime, handles multi-worker/multi-process setup (includes CORS and compression via Actix middleware)
   - `router.rs` - matchit-based routing (zero-copy path matching)
   - `handler.rs` - Python callback dispatcher via PyO3 with dual dispatch (sync/async paths via `DispatchOutcome` enum)
   - `response_meta.rs` - Response metadata types (`ResponseMeta`, `ResponseType`, `CookieData`) with static constants for zero-alloc fast path
   - `response_builder.rs` - Unified HTTP response building from `ResponseMeta` (content-type, headers, cookies all set in Rust)
   - `request.rs` - `PyRequest` struct with `OnceLock` lazy allocation for form/files/state/meta dicts
   - `request_pipeline.rs` - Typed parameter validation and caching
   - `validation.rs` - Auth and guard validation pipeline
   - `type_coercion.rs` - Rust-side parameter type coercion (UUID, Decimal, datetime, etc.)
   - `middleware/` - Custom middleware pipeline running in Rust (no Python GIL overhead)
     - `auth.rs` - JWT/API Key/Session authentication in Rust
     - `rate_limit.rs` - Token bucket rate limiting
   - `permissions.rs` - Guard/permission evaluation in Rust
   - `streaming.rs` - Streaming response handling (SSE, async generators)
   - `state.rs` - Shared server state (auth config, middleware config)
   - `metadata.rs` - Route metadata structures including `RustArgBinding` for Rust-side parameter extraction
   - `error.rs` - Error handling and HTTP exceptions
   - `form_parsing.rs` - Multipart and URL-encoded form parsing in Rust
   - `json.rs` - JSON body parsing
   - `cookies.rs` - Cookie parsing utilities
   - `cors.rs` - CORS handling
   - `static_files.rs` - Static file serving
   - `asgi_http.rs` - ASGI HTTP mounting support
   - `asgi_mounts.rs` - ASGI mount configuration

2. **Python Framework (`python/django_bolt/`)**

   - `api.py` - BoltAPI class with decorator-based routing (`@api.get/post/put/patch/delete/head/options`)
   - `binding.py` - Parameter extraction and type coercion
   - `responses.py` - Response types (PlainText, HTML, Redirect, File, FileResponse, StreamingResponse)
   - `exceptions.py` - HTTPException and error handling
   - `params.py` - Parameter markers (Header, Cookie, Form, File, Depends)
   - `dependencies.py` - Dependency injection system
   - `serialization.py` - msgspec-based serialization with ResponseWireV1 format and integer meta tags
   - `middleware_response.py` - `MiddlewareResponse` wrapper bridging internal wire format to middleware-compatible interface
   - `bootstrap.py` - Django configuration helper
   - `cli.py` - CLI tool for project initialization
   - `health.py` - Health check endpoints
   - `openapi.py` - OpenAPI schema generation
   - `pagination.py` - Pagination helpers (PageNumber, LimitOffset, Cursor)
   - `viewsets.py` - Class-based ViewSet and ModelViewSet
   - `auth/` - Authentication system
     - `guards.py` - Permission guards (IsAuthenticated, IsAdminUser, HasPermission, etc.)
     - `jwt_utils.py` - JWT utilities (create_jwt_for_user)
     - `token.py` - Token handling and validation
     - `revocation.py` - Token revocation stores (InMemoryRevocation, DjangoCacheRevocation, DjangoORMRevocation)
     - `middleware.py` - Middleware decorators (@cors, @rate_limit, @skip_middleware)
   - `middleware/compiler.py` - Compiles Python middleware config to Rust metadata
   - `management/commands/runbolt.py` - Django management command with autodiscovery

3. **Django Integration**
   - `runbolt` management command auto-discovers `api.py` files in:
     - Django project root (same directory as settings.py)
     - All installed Django apps (looks for `app_name/api.py`)
   - Merges all discovered BoltAPI instances into a single router
   - Supports standard Django ORM (async methods: `aget`, `afilter`, etc.)

### Request Flow

```
HTTP Request → Actix Web (Rust)
           ↓
    Route Matching (matchit - zero-copy)
           ↓
    Middleware Pipeline (Rust - no GIL)
      - CORS preflight/handling
      - Rate limiting (token bucket)
      - Compression (gzip/brotli/zstd)
           ↓
    Authentication (Rust - no GIL for JWT/API key/session validation)
      - JWT signature verification
      - Token expiration check
      - API key validation
      - Session validation (Django integration)
           ↓
    Guards/Permissions (Rust - no GIL)
      - IsAuthenticated, IsAdminUser, IsStaff
      - HasPermission, HasAnyPermission, HasAllPermissions
           ↓
    Dispatch (DispatchOutcome decision in handler.rs)
      ┌─────────────────────────────────────────────────────┐
      │ can_sync_dispatch=true?                              │
      │ (no middleware, no signals, has sync executor)       │
      ├──── YES ──────────────────────┬──── NO ─────────────┤
      │ SYNC PATH                     │ ASYNC PATH          │
      │ dispatch_sync.call1()         │ into_future_with    │
      │ Parse ResponseWireV1          │ _locals() coroutine │
      │ ┌─ body_kind?                 │ + asyncio event     │
      │ │ bytes → Ready(HttpResponse) │ loop polling        │
      │ │ stream/file → SyncResult    │                     │
      │ │   (carry parsed wire to     │                     │
      │ │    async post-processing)   │                     │
      │ └─ All in one GIL block       │                     │
      └───────────────────────────────┴─────────────────────┘
           ↓
    Parameter Extraction & Validation
      - Path params: {user_id} → function arg
      - Query params: ?page=1 → optional function arg
      - Headers: Annotated[str, Header("x-api-key")]
      - Cookies: Annotated[str, Cookie("session")]
      - Form: Annotated[str, Form("username")]
      - Files: Annotated[bytes, File("upload")]
      - Body: msgspec.Struct → validation
      - Dependencies: Depends(get_current_user)
           ↓
    Handler Execution
      - Sync handlers: direct call
      - Trivially-async handlers: coro.send(None) → StopIteration
      - True async handlers: await coroutine via asyncio
      - Django ORM access (async methods)
           ↓
    Response Serialization (ResponseWireV1 format)
      - Python returns 4-tuple: (status, meta, body_kind, body_payload)
      - Integer meta tags (0-3) → static Rust ResponseMeta (zero alloc)
      - Tuple meta for custom headers/cookies → Rust parses once
      - Body kinds: 0=bytes (zero-copy via PyBackedBytes), 1=stream, 2=file
      - Module-level msgspec.json.Encoder singleton
      - Inline dict/list fast path (bypasses serialize_response)
      - Response model validation if specified
           ↓
    Response Compression (if enabled)
      - Client-negotiated (Accept-Encoding)
      - gzip/brotli/zstd support
           ↓
    HTTP Response (back to Actix Web)
```

### Performance Characteristics

- **Authentication/Guards run in Rust**: JWT validation, API key checks, and permission guards execute without Python GIL overhead
- **Zero-copy routing**: matchit router matches paths without allocations
- **Batched middleware**: Middleware (CORS, rate limiting, compression) runs in a pipeline before Python handler is invoked
- **Sync dispatch bypass**: Eligible handlers skip the async bridge entirely (~6-12μs savings per request)
- **Trivially-async optimization**: `async def` with no `await` detected via bytecode analysis, dispatched synchronously
- **Zero-copy response body**: `PyBackedBytes` → `Bytes::from_owner()` eliminates memcpy for serialized response bodies
- **Integer meta tags**: Common response types (JSON, plaintext, etc.) use integer tags mapped to static Rust constants — zero allocation per response
- **Cookie serialization in Rust**: Raw cookie tuples pass from Python → Rust, eliminating Python `SimpleCookie` overhead
- **Lazy request allocations**: Form/files/state/meta dicts use `OnceLock`, only allocated when accessed (~95% of requests save 2-4 dict allocations)
- **Multi-process scaling**: SO_REUSEPORT allows kernel-level load balancing across processes
- **msgspec serialization**: 5-10x faster than standard JSON for request/response handling
- **Efficient compression**: Client-negotiated gzip/brotli/zstd compression in Rust

## API Usage Documentation

For API development patterns, usage examples, and integration guides, see the [documentation](docs/README.md):

- **[Getting Started Guide](docs/GETTING_STARTED.md)** - Tutorial for building APIs with Django-Bolt
- **[Security Guide](docs/SECURITY.md)** - Authentication, authorization, CORS, rate limiting
- **[Middleware Guide](docs/MIDDLEWARE.md)** - CORS, rate limiting, custom middleware
- **[Responses Guide](docs/RESPONSES.md)** - All response types and streaming
- **[Class-Based Views](docs/CLASS_BASED_VIEWS.md)** - ViewSet and ModelViewSet patterns
- **[OpenAPI Guide](docs/OPENAPI.md)** - Auto-generated API documentation
- **[Pagination Guide](docs/PAGINATION.md)** - PageNumber, LimitOffset, Cursor pagination
- **[Logging Guide](docs/LOGGING.md)** - Request/response logging and metrics
- **[Full Documentation Index](docs/README.md)** - Complete list of all documentation

## Testing Strategy

### Unit Tests

Located in `python/tests/`:

**Core Functionality**:

- `test_syntax.py` - Route syntax, parameter extraction, response types
- `test_decorator_syntax.py` - Decorator-based route definitions
- `test_parameter_validation.py` - Parameter validation logic
- `test_json_validation.py` - JSON request/response validation
- `test_integration_validation.py` - End-to-end validation tests
- `test_file_response.py` - File download and streaming

**Authentication & Authorization**:

- `test_jwt_auth.py` - JWT authentication logic
- `test_jwt_token.py` - Token generation and validation
- `test_guards_auth.py` - Guard/permission logic
- `test_guards_integration.py` - Integration tests for guards
- `test_auth_secret_key.py` - Secret key handling

**Middleware & CORS**:

- `test_middleware.py` - Middleware system tests
- `test_global_cors.py` - Global CORS configuration
- `test_middleware_server.py` - Middleware integration tests

**Advanced Features**:

- `test_error_handling.py` - Error handling and exceptions
- `test_logging.py` - Request/response logging
- `test_logging_merge.py` - Logging configuration merging
- `test_health.py` - Health check endpoints
- `test_openapi_docs.py` - OpenAPI documentation generation
- `test_pagination.py` - Pagination helpers (PageNumber, LimitOffset, Cursor)
- `test_testing_utilities.py` - Testing utilities and test client

**Class-Based Views**:

- `cbv/test_viewset_unified.py` - ViewSet pattern tests
- `cbv/test_model_viewset.py` - ModelViewSet pattern tests
- `cbv/test_action_decorator.py` - Custom action decorators

**Django Integration**:

- `admin_tests/` - Django admin integration tests
- `test_models.py` - Django model integration

### Test Servers

Test infrastructure uses separate server files:

- `syntax_test_server.py` - Routes for testing basic functionality
- `middleware_test_server.py` - Routes for testing middleware
- Server instances are started in subprocess for integration tests

### Running Tests

Always run tests with `-s -vv` for detailed output:

```bash
uv run --with pytest pytest python/tests -s -vv
```

## Common Development Tasks

### After Modifying Rust Code

1. Run `just build` to rebuild the Rust extension
2. Run tests: `just test-py`
3. Optionally run benchmarks: `just save-bench`

### After Modifying Python Code

1. Run tests: `just test-py` (tests will fail if modifications break functionality)
2. No rebuild needed (Python is interpreted)
3. For middleware/auth changes, test both in isolation and with integration tests

### Adding a New Core Feature

1. Identify the component location (auth, middleware, responses, etc.)
2. Implement in both Python (`python/django_bolt/`) and Rust (`src/`) if needed
3. Add comprehensive tests in `python/tests/`
4. Update relevant documentation in `docs/`
5. Run full test suite: `just test-py`

### Adding a New Authentication Backend

1. Extend `python/django_bolt/auth/backends.py` with new backend class
2. Implement validation logic (can run in Rust via `src/middleware/auth.rs` if performance-critical)
3. Add tests in `python/tests/test_*_auth.py`
4. Update `python/django_bolt/auth/__init__.py` exports
5. Document in [docs/SECURITY.md](docs/SECURITY.md)

### Debugging Performance Issues

1. Run `just save-bench` to establish baseline
2. Make changes
3. Run `just save-bench` again (rotates baseline, creates new dev benchmark)
4. Compare BENCHMARK_BASELINE.md vs BENCHMARK_DEV.md
5. Key metrics: Requests per second, Failed requests

## Supported Versions

- **Python**: 3.10, 3.11, 3.12, 3.13
- **Django**: 4.2, 5.0, 5.1
- **PyPy**: Supported (performance slower than CPython)

## Project Structure (python/django_bolt)

### Core API

- `api.py` - Main BoltAPI class and route decorators
- `router.py` - Route registration and management
- `binding.py` - Parameter extraction and type coercion from requests

### HTTP Handling

- `responses.py` - Response types (PlainText, HTML, File, Streaming, etc.)
- `exceptions.py` - HTTPException and error handling
- `params.py` - Parameter markers (Header, Cookie, Form, File, Depends)

### Serialization & Validation

- `serialization.py` - msgspec-based serialization, ResponseWireV1 format, integer meta tag constants (`_RESPONSE_META_JSON`, etc.), body kind constants (`_BODY_BYTES`, `_BODY_STREAM`, `_BODY_FILE`)
- `middleware_response.py` - `MiddlewareResponse` wrapper bridging wire tuples to middleware-compatible objects
- `param_functions.py` - Dynamic parameter handling

### Authentication & Security

- `auth/backends.py` - JWT and API Key implementations
- `auth/guards.py` - Permission guards (IsAuthenticated, HasPermission, etc.)
- `auth/jwt_utils.py` - Token creation helpers
- `auth/token.py` - Token validation logic
- `auth/user_loader.py` - Django User integration
- `auth/revocation.py` - Token revocation stores

### Middleware

- `middleware/middleware.py` - CORS, rate limiting decorators
- `middleware/compiler.py` - Compiles Python config to Rust metadata
- `middleware/compression.py` - Response compression utilities

### Advanced Features

- `health.py` - Health check endpoints
- `openapi/` - OpenAPI/Swagger schema generation
- `pagination.py` - Pagination helpers
- `viewsets.py` - Class-based ViewSet and ModelViewSet
- `testing/` - Test utilities and client
- `logging/` - Request/response logging
- `dependencies.py` - Dependency injection system

### Django Integration

- `bootstrap.py` - Django configuration helper
- `cli.py` - CLI tool (`django-bolt version` command)
- `management/commands/runbolt.py` - Django management command with autodiscovery
- `apps.py` - Django app configuration

## Troubleshooting (Development)

### Build fails after modifying Rust code

**Problem**: `just build` fails with compilation errors

**Solution**:

1. Check Rust syntax: `cargo check`
2. Review error message for specific file/line
3. Ensure PyO3 types are correct in Rust code
4. Try clean rebuild: `just rebuild`
5. Check Rust version compatibility: `rustc --version` (should support PyO3)

### Tests fail inconsistently

**Problem**: Some tests pass sometimes, fail other times

**Solution**:

1. Always run with `-s -vv` flags for detailed output: `just test-py`
2. Check for async/await issues - ensure all async code awaits properly
3. Look for race conditions in concurrent test execution
4. Verify database state between tests (use fixtures/conftest)
5. Check test isolation - tests shouldn't depend on execution order

### Performance regression after changes

**Problem**: RPS drops significantly after code modifications

**Solution**:

1. Run benchmarks to quantify: `just save-bench` (creates baseline comparison)
2. Check which component regressed: compare BENCHMARK_BASELINE.md vs BENCHMARK_DEV.md
3. Profile Rust hot paths: check `src/handler.rs`, `src/response_builder.rs`, `src/response_meta.rs`, `src/router.rs`
4. Check for GIL contention in Python: look at `src/handler.rs` for Python interop efficiency
5. Review recent commits for inefficient patterns (N+1 queries, unnecessary allocations, etc.)
6. Use benchmarking to isolate: test individual endpoints

### Rust-Python interop issues

**Problem**: PyO3 bridge errors or segmentation faults

**Solution**:

1. Review PyO3 error messages carefully - they're usually descriptive
2. Check `src/handler.rs` for correct GIL handling
3. Ensure Python object references are properly managed (don't hold across GIL releases)
4. Test in isolation with minimal example
5. Check PyO3 version compatibility in `Cargo.toml`

## Important Implementation Notes

### Core Framework Design

- **Dual dispatch architecture**: Handlers are dispatched via three outcomes in `src/handler.rs`, controlled by `DispatchOutcome` enum:
  - **Sync path** (`DispatchOutcome::Ready`): Calls `dispatch_sync.call1()`, parses `ResponseWireV1`, builds `HttpResponse` — all in one `Python::attach` GIL block. Used when `can_sync_dispatch` flag is set in `RouteExecutionPlan` and body is bytes.
  - **Sync + async post-processing** (`DispatchOutcome::SyncResult`): Handler runs synchronously but returns a stream/file body that needs async handling. Carries the already-parsed `ParsedResponseWire` to avoid re-acquiring GIL.
  - **Async path** (`DispatchOutcome::Pending`): Creates coroutine via `dispatch.call1()`, converts to Rust future via `into_future_with_locals()`. Used for handlers with middleware, signals, or true async awaits.
- **Trivially-async handlers**: `async def` functions that never `await` are detected at registration via `dis.get_instructions()` (checking for `GET_AWAITABLE` opcode). These get a sync executor that drives the coroutine via `coro.send(None)` → `StopIteration`, avoiding the async bridge entirely.
- **ResponseWireV1 format**: Python returns `(status, meta, body_kind, body_payload)` — meta is either an integer tag (fast path) or a 4-tuple `(response_type, custom_ct, headers, cookies)` (slow path). Body kind: 0=bytes, 1=stream, 2=file.
- **Zero-copy response body**: `PyBackedBytes` holds a reference to Python bytes. `Bytes::from_owner()` wraps it without memcpy, used for JSON/serialized bodies.
- **MiddlewareResponse bridge**: `python/django_bolt/middleware_response.py` wraps internal wire tuples into middleware-compatible objects with `.status_code` and `.headers` attributes. Raw cookie tuples are preserved (not serialized in Python) — Rust handles all cookie serialization.
- **Python-Rust bridge (PyO3)**: Handler execution crosses the GIL boundary. Minimize Python work in Rust hot paths. See `src/handler.rs` for GIL management patterns.
- **Middleware compilation**: Middleware decorators on handlers are converted to Rust metadata structs at server startup. Implementation in `python/django_bolt/middleware/compiler.py` and `src/metadata.rs`.
- **Route autodiscovery**: Runs once at startup. No hot-reload in production (only in `--dev` mode). See `python/django_bolt/management/commands/runbolt.py`.
- **Multi-process isolation**: Each process has independent Python interpreter and Django imports. State sharing must happen via Rust shared state (`src/state.rs`) or external mechanisms.

### Development Standards

- **Never silently ignore errors**: Always print or raise exceptions - silent failures create obscure bugs that are hard to trace
- **Tests must be meaningful**: Only add tests that verify actual functionality; tests must fail when functionality changes
- **Prefer `from __future__ import annotations`**: Use PEP 563 for cleaner type hints across Python files
- **Imports at top**: Always place imports at module top, never inline imports
- **Test-driven discipline**: Don't remove failing test asserts or skip tests to just them pass - report the failure and investigate the root cause

## When to Edit Which Files

### Adding Framework Features

- **New parameter types** (Query, Header, Body variants): Edit `python/django_bolt/params.py` and `python/django_bolt/binding.py`, then update `src/handler.rs` for extraction
- **New response types**: Add to `python/django_bolt/responses.py`, implement serialization in `python/django_bolt/serialization.py`, and if adding a new common type, add integer meta tag constant there + matching static `ResponseMeta` in `src/response_meta.rs`
- **New authentication backend**: Extend `python/django_bolt/auth/backends.py` with new class, optionally implement validation in `src/middleware/auth.rs` for performance
- **New guard/permission type**: Add to `python/django_bolt/auth/guards.py` and implement check in `src/permissions.rs`
- **New middleware system**: Add to `python/django_bolt/middleware/`, implement compiler in `middleware/compiler.py`, and Rust handler in `src/middleware/`

### Performance Improvements

- **Serialization speed**: `python/django_bolt/serialization.py` and `src/handler.rs`
- **Response building**: `src/response_builder.rs` (header/cookie/content-type setting) and `src/response_meta.rs` (static meta constants)
- **Routing performance**: `src/router.rs`
- **GIL contention**: Reduce Python work in hot paths, consider moving logic to Rust in `src/handler.rs`
- **Request allocation**: `src/request.rs` (lazy dict allocation via `OnceLock`)
- **Middleware response overhead**: `python/django_bolt/middleware_response.py` (wire format bridge)
- **Compression**: `python/django_bolt/middleware/compression.py` and `src/middleware/compression.rs`

## Performance Principles (Hot Path)

The core performance principle is **do it once at registration, reuse forever at runtime**. Every per-request operation must justify its existence.

### Registration-Time Pre-computation (MUST DO)

All route metadata keys must be guaranteed at registration time so the dispatch path uses direct `meta["key"]` access, never `meta.get("key", default)`. This applies to `_route_decorator()`, `compile_binder()`, and any code that registers routes outside the normal decorator path (admin routes, static routes, mounted apps). Required keys: `mode`, `is_async`, `default_status_code`, `response_type`.

### Hot Path Rules

When modifying code in the per-request dispatch path (`api.py:_dispatch`, `api.py:_dispatch_sync`, `serialization.py:serialize_response`, `_kwargs/model.py` injectors, `dependencies.py`):

1. **No string dispatch in loops** -- Pre-sort fields into source buckets (path, query, header, cookie, form) at registration time. Iterate pre-built lists at request time instead of `if source == "path"` chains.
2. **Pre-compute singleton tuples/headers** -- Response metadata tuples (`_RESPONSE_META_JSON`, error headers) should be module-level constants, not rebuilt per response.
3. **Avoid per-request allocations** -- Don't create temporary dicts/lists that can be eliminated. Example: pagination should use PyRequest directly instead of copying into a new dict.
4. **Single `msgspec.convert()` for validation** -- Prefer one C-accelerated `msgspec.convert()` call over field-by-field Python validation when possible (Litestar pattern: `msgspec.defstruct()` at registration, single convert at runtime).
5. **Pre-compute file field names** -- Don't iterate all struct fields at request time to find UploadFile fields. Store file field names at registration time.
6. **Use `__slots__`** -- On request wrapper, state, and connection objects to avoid per-instance `__dict__` allocation.
7. **Cache parsed data** -- Use `@cached_property` on middleware config objects. Consider scope-level caching so middleware and handler share parsed query params/cookies.
8. **Singleton encoders** -- Use module-level `msgspec.json.Encoder(enc_hook=...)` instances, don't recreate per response.
9. **Parallel dependency resolution** -- Pre-compute dependency graph at registration. Execute independent deps concurrently with `asyncio.gather()`.
10. **Pre-bind serializers** -- Use `functools.partial` to bind handler-specific serializer config at registration, avoiding per-call config lookups.

### PyO3 Async Bridge Optimization

The `pyo3_async_runtimes::into_future_with_locals()` call adds ~6-12μs per request (coroutine creation + asyncio event loop polling). This is the single biggest per-request overhead for simple handlers. Strategies to eliminate it:

1. **Sync dispatch bypass** -- For handlers with no middleware/signals, Rust calls `dispatch_sync.call1()` directly within a single `Python::attach` GIL block. Returns `DispatchOutcome::Ready(HttpResponse)` instead of `DispatchOutcome::Pending(Future)`. Controlled by `can_sync_dispatch` flag in `RouteExecutionPlan` bitfield, computed at registration.

2. **Trivially-async detection** -- Use `dis.get_instructions()` at registration time to check for `GET_AWAITABLE` opcode. If absent, the `async def` never actually awaits and can be driven synchronously via `coro.send(None)` → `StopIteration`. Set `meta["_sync_executor"]` for these handlers so they go through the sync dispatch path in Rust.

3. **When to use sync dispatch** -- Only when ALL conditions are met: handler has `_sync_executor`, no Python middleware (global or route), no Django middleware, no signals. If any condition fails, fall back to full async path.

### Response Wire Format Optimization

The Python→Rust response wire format `(status, meta, body_kind, body)` (ResponseWireV1) has two meta encoding paths and three body encoding paths:

**Meta encoding:**

1. **Integer meta tags (fast path)** -- Common response types use integer tags (0=JSON, 1=plaintext, 2=octetstream, 3=empty) defined in `serialization.py`. Rust maps these to static `ResponseMeta` constants in `response_meta.rs` via `MetaRef::Static(&'static ResponseMeta)` — zero allocation, zero tuple parsing.

2. **Tuple meta (slow path)** -- Responses with custom headers/cookies use full `(response_type, custom_ct, headers, cookies)` tuples parsed into `MetaRef::Owned(ResponseMeta)`. Cookie tuples are 9-element tuples `(name, value, path, max_age, expires, domain, secure, httponly, samesite)` — Rust serializes them to `Set-Cookie` headers directly.

**Body encoding (body_kind integer):**

- `0` (_BODY_BYTES) -- bytes payload. Rust uses `PyBackedBytes` → `Bytes::from_owner()` for zero-copy when possible, falls back to `Vec<u8>` extraction.
- `1` (_BODY_STREAM) -- StreamingResponse object. Rust creates SSE or chunked stream from Python async generator.
- `2` (_BODY_FILE) -- File path string. Rust opens and streams the file asynchronously via tokio.

**Middleware compatibility:** `MiddlewareResponse` class in `middleware_response.py` converts between wire tuples and middleware-friendly objects. It preserves raw cookie tuples (never serializes in Python) so Rust handles all cookie serialization.

When adding new common response types, add both a Python integer constant and a Rust static constant, and keep them in sync.

### What NOT to Do on the Hot Path

- `hasattr()` checks (use `__init__` to set attributes)
- `meta.get()` with defaults (guarantee keys at registration)
- `isinstance()` after a conversion that guarantees the type
- Double dict lookups (`if d.get(k): v = d[k]` -- use walrus or single `.get()`)
- String parsing in error formatting when structured data is available
- Duplicate async/sync code paths -- extract shared logic into helpers
- Thread-local encoders when module-level singletons work (GIL guarantees thread safety)
- Per-response tuple construction for common response types (use integer meta tags)

### Testing

- **Add unit tests**: Create file in `python/tests/test_*.py` following existing patterns
- **Add integration tests**: Modify `python/tests/syntax_test_server.py` or `python/tests/middleware_test_server.py` for test routes
- **Test Rust changes**: Add tests in `src/test_state.rs` and `src/testing.rs`
- Always import on the top of the file

## Testing Principles

### Tests Must Fail Without the Fix

When adding tests for bug fixes, the test MUST fail when the fix is reverted. This ensures:

1. The test actually validates the fix, not some unrelated behavior
2. The test would have caught the bug if it existed before
3. We're not testing bogus claims that pass regardless of the fix

**How to verify**: Before finalizing a bug fix test, revert the fix and confirm the test fails. Only then apply the fix and confirm the test passes.

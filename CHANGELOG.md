# Changelog

All notable changes to this project will be documented in this file.

## [0.8.2]

### Added

- **URL reversing for Bolt routes** - Include `django_bolt.urls` in your `ROOT_URLCONF` (`path("", include("django_bolt.urls"))`) and Django's `reverse()`, `reverse_lazy()`, and the `{% url %}` template tag resolve Bolt route names. Entries are reverse-only — Bolt still serves the paths in Rust and the registered views never run; path converters, `args`/`kwargs`, `query`, and `fragment` come from Django's own resolver.
- **`name=` on every route decorator** - `@api.get/post/put/patch/delete/head/options`, `@api.websocket`, `@api.view`, `@api.viewset`, and `@action` accept an explicit reverse name. Unnamed routes derive a name verbatim from the Python identifier (function name, or class name for class-based views); viewsets name each route `{base}-{action}` (e.g. `user-list`, `user-partial_update`).
- **Opt-in reverse namespaces** - `BoltAPI(namespace="...")` mirrors Django's `app_name`; namespaced routes reverse as `namespace:name` and resolve only under that namespace.

### Documentation

- **Routing docs** - New "URL names and reversing" section in `routing.md` covering wiring, naming, derived names, namespaces, viewset/`@action` naming, and collision rules; `class-based-views.md` documents the `@action` `name=` parameter and cross-links it.

## [0.8.1]

### Added

- **Native media serving** - With `MEDIA_URL` + `MEDIA_ROOT` set, uploads are served from Rust: `GET`/`HEAD`, ETag/Last-Modified, conditional (`304`) and range requests.
- **Native static serving** - `/static` now uses the same Rust handler (Python static route removed), resolving via `STATIC_ROOT`/`STATICFILES_DIRS`, with staticfiles finders as a `DEBUG`-only fallback.
- **`BOLT_STATIC_MAX_AGE` / `BOLT_MEDIA_MAX_AGE`** - Emit `Cache-Control` on `2xx`: `public` for static, `private` for media. Validated into a pre-built header at startup.
- **Optional CSP on file responses** - `SECURE_CSP`, if set, applies `Content-Security-Policy` to static and media responses (including errors).
- **Union response types** - Handlers can declare union (`X | Y`) return types. (#228)
- **Sequence form fields** - Repeated form keys bind to `list[T]` struct fields, with list emission handled Rust-side.
- **Per-chunk streaming compression** - `StreamingResponse`/`EventSourceResponse` compress per chunk with a sync flush (events reach the client immediately); one encoder per connection preserves cross-chunk ratio. brotli/gzip/zstd; opt out with `@no_compress`.
- **Unified `CompressionConfig`** - One config drives both buffered and streaming compression, sharing `@no_compress` and `Accept-Encoding` negotiation.

### Security

- **Script-bearing uploads force-downloaded** - Media with script-capable extensions (`.html`, `.svg`, `.js`, `.xml`, `.wasm`, …) is served as `application/octet-stream` + `Content-Disposition: attachment`. Inert types (images, PDF, CSS, JSON, text) still render inline. Media only — static keeps native content types.
- **`X-Content-Type-Options: nosniff` on every file response**, including 404s.
- **Traversal/dotfile/symlink protections** - `..` paths rejected with `400`; leading-dot components (`.env`, `.git/config`, …) return `404`; symlink targets must stay inside the root.
- **No static/media route collisions** - Media misses no longer fall through to staticfiles finders, so `STATICFILES_DIRS` assets can't leak under `/media/`.
- **Media config validated at startup** - `MEDIA_URL` must start with `/`; `MEDIA_ROOT` must be absolute.

### Changed

- **Unified static/media config** - Collapsed into one `ScopeConfig` tagged by a `ServeMode` enum (one `app_data` lookup per request). `HEAD` registered for both scopes, mirroring GET headers.
- **`brotli_lgwin` default 18 → 14** - 16 KiB window (was 256 KiB) cuts per-stream memory ~16× at minimal ratio cost. Tune up for large repetitive bodies, down for high-fanout streams.
- **Buffered Accept-Encoding negotiation now RFC 7231 §5.3.4-compliant** - Shares the streaming parser: honors q-values (`br;q=0`), the `*` wildcard, and case-insensitive tokens. The old substring matcher mis-handled all three.

### Fixed

- **`BoltAPI(compression=False)` now disables buffered compression** - Previously it still fell back to defaults and compressed anyway; the negotiator now returns `identity` when no config is attached. (`compression=None`/omitted still applies the default `CompressionConfig()`.)

### Documentation

- **New `media-files.md`** - Upload security model, `Cache-Control`, and production offloading (Nginx / object storage). `static-files.md` and settings reference updated for native serving and `BOLT_*_MAX_AGE`.
- **New `compression.md`** - Buffered + streaming compression documented together (replaces `streaming_compression.md`): `CompressionConfig`, negotiation, per-chunk flush, `lgwin` memory table, level/ratio tradeoffs, CRIME/BREACH. `middleware.md` shortened to link here.

## [0.7.5]

### Added

- **`EventSourceResponse` for Server-Sent Events** - FastAPI-style SSE response type with automatic event framing and reconnection support. (#203)
- **Class-based views improvements** - `ViewSet`/`ModelViewSet` refinements alongside built-in pagination support for class-based list actions. (#189)

### Fixed

- **`Depends()` targets that read `request.query`/`.headers`/`.cookies`/`.body`** - Registration-time static analysis now recurses into `Depends()` targets (including class-callable backends like `Depends(FilterBackend(...))`), so a dep that accesses request components no longer sees an empty dict. Rust was silently skipping request-component parsing for routes whose handler didn't directly reference `request.*` even though a dep did.
- **Serializer `model_validate` now reports missing required fields** - Validation errors now include the names of missing required fields instead of a generic message. (#201)
- **OpenAPI schema unwraps `_FieldMarker` defaults** - Generated OpenAPI schemas no longer leak internal `_FieldMarker` objects as serializer defaults.

## [0.6.4]

### Added

- **ASGI support for Django views and URL mounts** - Added ASGI mounting support for Django views/URLs with updates in routing, middleware response handling, server integration, and test client behavior. (#145)
- **Rust-side argument prebinding on hot path** - Added Rust prebinding path to reduce Python injector overhead during request handling.
- **Structured `runbolt` startup banner** - Redesigned startup output with clearer structured runtime information.

### Changed

- **Core hot-path optimizations** - Reduced unnecessary cloning/parsing and improved sync serialization and request pipeline performance.
- **Parameter extraction updates for msgspec annotations** - Improved extraction behavior and middleware compilation for msgspec-based annotations.
- **Testing/runtime dependencies** - Added `httpx` to installation dependencies and aligned testing docs with the runtime setup.

### Fixed

- **OpenAPI nested serializer schema generation** - Fixed nested serializer schema rendering in generated OpenAPI docs. (#144)
- **Multiprocess shutdown handling** - Fixed process shutdown behavior for multiprocess `runbolt` execution.
- **Static file serving** - Fixed static file serving behavior in fresh installs and removed bundled example admin staticfiles to rely on the proper Django static pipeline.
- **Minor typo fix** - Corrected typo. (#138)

### Documentation

- Added ASGI mounts documentation and updated API/routing/settings references.
- Fixed repository URL in README. (#148)
- Corrected Agents file naming and additional docs assumption fixes.

## [0.6.0]

### Added

- **Actix-native static file serving** - Serve static files directly from Actix without Python handler overhead. Reads Django settings (`STATIC_URL`, `STATIC_ROOT`, `STATICFILES_DIRS`) at startup and uses actix-files with proper ETag, Last-Modified, and MIME type handling. Includes Content Security Policy header support, directory traversal prevention, symlink security, and falls back to Django staticfiles finders in debug mode only. (#123)
- **Session authentication** - Django session framework integration with async session methods (`request.session.aget()`, `aset()`, `apop()`, `aflush()`, etc.) for non-blocking session access. Includes login/logout endpoint examples and browser-based session demo. Removed the deprecated `SessionAuthentication` class in favor of using Django's built-in session middleware directly. (#112)
- **Global authentication and permission classes** - New `BOLT_AUTHENTICATION_CLASSES` and `BOLT_PERMISSION_CLASSES` Django settings to configure project-wide defaults instead of specifying them on every endpoint. (#121 (Docs Changes))
- **camelCase/snake_case field mapping in serializers** - Serializers now support automatic field name transformation via `rename="camel"` in the Config class, allowing snake_case Python attributes to serialize as camelCase JSON and vice versa. (#115)
- **`request.META` compatibility** - Added `request.META` dictionary populated in Rust with HTTP headers (as `HTTP_*` keys), server info (`SERVER_NAME`, `SERVER_PORT`), and request metadata (`REQUEST_METHOD`, `PATH_INFO`, `QUERY_STRING`, `CONTENT_TYPE`, `CONTENT_LENGTH`). Enables easier migration from Django views and compatibility with libraries expecting Django request objects. (#128)

### Changed

- **Cookie and header parsing moved to Rust** - Cookie parsing now uses the actix-cookie crate in Rust instead of Python-side parsing. Added `response.set_cookie()` shortcut method with support for all cookie attributes (max_age, expires, path, domain, secure, httponly, samesite). Removed Python-side cookie serialization for better performance. (#127)
- **Removed legacy middleware configuration** - Removed deprecated `middleware` parameter format from `BoltAPI` constructor that accepted raw middleware config dicts. Middleware should now be passed as classes or the `@cors`/`@rate_limit` decorators. (#120)
- **Middleware safety classification** - `DjangoMiddlewareStack` now automatically classifies Django middleware as safe (async-compatible) or unsafe (blocking I/O) based on known patterns. Added `auser` property setter on Request for compatibility with Django's `alogin()` and `alogout()` functions. (#93)

### Fixed

- **Streaming response handling in middleware** - Fixed `TypeError: cannot unpack non-iterable StreamingResponse object` when using `StreamingResponse` with middleware. The serialization layer now correctly returns streaming responses as tuples, and both `handler.rs` and `testing.rs` detect and process `StreamingResponse` bodies appropriately while preserving SSE-specific headers. Added `PyOnceLock`-based caching to avoid repeated imports on the streaming path. (#126)
- **Pagination respects serializer fields** - Pagination now correctly uses serializer field definitions for item serialization instead of raw model fields. Added `extract_pagination_item_type()` helper and enhanced `PaginatedResponse` with `omit_defaults=True` for cleaner output. (#100)
- **`from_model()` respects `field(source=...)` parameter** - Fixed serializer's `from_model()` ignoring fields defined with `source="..."`. Now checks `__source_mapping__` and correctly retrieves values using the source path, including dot-notation nested access (e.g., `source="author.name"`). Also improved Python 3.14 compatibility using `inspect.get_annotations()` for PEP 649 deferred annotation evaluation. (#107)

### Documentation

- Clarified global vs per-view file upload size limits with examples. (#102)
- Added comprehensive static files documentation with CSP configuration.
- Added session authentication setup guide with login/logout examples.
- Enhanced pagination documentation with serializer integration patterns.

### New Contributors

- @NourEldin-Osama
- @Rey092
- @athul-binu

## [0.5.1]

### Changed

- **Starlette-style trailing slash redirects** - When a request URL doesn't match a route, Django-Bolt now checks if the alternate path (with/without trailing slash) exists. If found, returns a **308 Permanent Redirect** to the canonical URL. This means both URLs work, with the non-canonical one redirecting.
- **Mixed trailing_slash settings** - When auto-discovering multiple `api.py` files, each API's routes now keep their own `trailing_slash` format. Different apps can use different conventions without conflict.

### Fixed

- **Trailing slash with multiple APIs** - Fixed issue where APIs with different `trailing_slash` settings would conflict when merged. Each API now respects its own setting.

### Documentation

- Updated routing docs with trailing slash redirect behavior and multi-API examples.

## [0.5.0]

### Added

- **Trailing slash configuration** - New `trailing_slash` parameter for `BoltAPI` to control path normalization:
  - `"strip"` (default): Remove trailing slashes (`/users/` → `/users`)
  - `"append"`: Add trailing slashes (`/users` → `/users/`)
  - `"keep"`: No normalization, keep paths as defined
- **URL prefix support** - New `prefix` parameter for `BoltAPI` to apply a URL prefix to all routes (e.g., `/api/v1`).
- **Memory allocator options** - Optional jemalloc and mimalloc support via feature flags for improved memory performance.

### Performance

- **Type coercion moved to Rust** - Path, query, and header parameter type coercion now happens in Rust before reaching Python.
- **Form parsing in Rust** - Multipart form data and URL-encoded body parsing moved to Rust for faster file uploads and form handling.
- Combined, these changes provide **10-60% performance improvement** depending on endpoint complexity.
- **Interned Python strings** - Attribute access in Rust hot paths uses interned strings for faster lookups.

### Fixed

- **OpenAPI authorize button** - Fixed authorize button not showing for routes with authentication in Swagger UI.
- **OpenAPI docs with prefix** - Documentation routes (`/docs/*`) now work correctly when API has a prefix, staying at absolute paths.
- **Mount path normalization** - Mount paths without leading slash are now properly normalized.

### Changed

- **Refactored parameter extraction** - Consolidated `binding.py` into `_kwargs` module with pre-compiled extractors for better performance and maintainability.
- **Updated jsonwebtoken to v10** - Upgraded Rust JWT library.

## [0.4.8]

### Added

- **UploadFile class** - Django-compatible file upload handling with automatic resource cleanup, size validation, and content type checking.
- **FileSize enum** - Human-readable file size constants (`FileSize.MB(10)`, `FileSize.GB(1)`) for upload limits.
- **MediaType enum** - Content type constants for response handling.
- **Tags support for views** - Added `tags` parameter to `@api.view()` and `@api.viewset()` decorators for OpenAPI grouping.

### Changed

- **Documentation improvements** - Updated routing and cookie documentation.

## [0.4.7]

### Performance

- **Lazy user loading optimization** - Replaced lambda with `functools.partial` for `SimpleLazyObject` user loader, avoiding closure allocation overhead per authenticated request.
- **Response serialization fast path** - Added dedicated fast path for dict/list responses (90%+ of handlers) that skips `_convert_serializers()` check and unnecessary isinstance chain.

## [0.4.6]

### Added

- **Multi-error validation collection** - Serializer now collects ALL validation errors before raising, matching Pydantic's behavior. Both `@field_validator` and `@model_validator` errors are collected across all fields.
- **Meta constraint multi-error collection** - `model_validate()` and `model_validate_json()` now collect all msgspec Meta constraint errors (min_length, pattern, ge, le, etc.) using Litestar's field-by-field validation approach.
- **Parameter models** - Support for `Annotated[Struct/Serializer, Form()]` pattern like FastAPI. Group related form fields, query parameters, headers, or cookies into a single validated object using `msgspec.Struct` or `Serializer`.
  - `Annotated[FormModel, Form()]` - Group form fields with validation
  - `Annotated[QueryModel, Query()]` - Group query parameters
  - `Annotated[HeaderModel, Header()]` - Group headers (snake_case fields map to kebab-case headers)
  - `Annotated[CookieModel, Cookie()]` - Group cookies
- **Testing documentation** - Comprehensive testing guide covering TestClient usage, database transactions, and integration testing patterns.
- **Serializer vs msgspec.Struct documentation** - New docs section explaining differences in error handling between raw msgspec.Struct and Django-Bolt Serializer.

### Changed

- **204 No Content support** - Framework now properly handles `None` return values for endpoints with `status_code=204`. DELETE endpoints should return nothing with 204 status.

### Fixed

- **500 error on 204 responses** - Fixed server error when handlers returned `None` for 204 No Content responses.
- **Validation errors now return 422** - Missing required parameters (query, header, cookie, form, file) now return 422 Unprocessable Entity instead of 400 Bad Request, per RFC standards.

### Removed

- **`django-bolt init` command** - Removed CLI initialization command. The CLI now only provides the `version` command.

## [0.4.0]

### Changed

- **Python 3.12+ required** - Dropped support for Python 3.10 and 3.11, now requires Python 3.12 or newer.
- **Modern Python syntax** - Adopted PEP 695 generic syntax, `datetime.UTC`, and native `NotRequired` from Python 3.12+.
- **PyO3 ABI update** - Updated from `abi3-py310` to `abi3-py312` for improved Rust-Python interop.

## [0.3.13]

### Added

- **Django middleware integration** - Full support for Django's middleware pattern with automatic loading from `settings.MIDDLEWARE`.
- **DjangoMiddleware adapter** - Seamlessly wrap and use existing Django middleware in Bolt applications.
- **Middleware loader** - Automatic discovery and loading of Django middleware with configurable selection.
- **Ruff linting and type checking** - Added comprehensive code quality tooling with pyproject.toml configuration.

### Changed

- **Unified middleware pattern** - Middleware now uses Django's `__init__(get_response)` and `__call__(request)` pattern for consistency.
- **Middleware architecture** - Complete redesign following the middleware design document with zero overhead as priority.
- **Router enhancements** - Added `middleware`, `auth`, and `guards` parameters for router-level configuration with inheritance support.
- **Response builder optimizations** - Refactored response building pipeline in Rust with dedicated modules (`response_builder.rs`, `responses.rs`, `headers.rs`).
- **Code quality improvements** - Applied Ruff linting across entire codebase, improved type hints and imports.

### Fixed

- **Middleware error handling** - Improved exception catching and proper response generation in middleware pipeline.
- **Error handling improvements** - Enhanced error messages to include original exception context for better debugging.
- **Redirect Error in docs and admin** - Fixed Error because of path normalization that we added because websocket.

## [0.3.12]

### Added

- **WebSocket parameter injection** - Pre-compiled injectors for improved WebSocket parameter handling (query, header, and cookie parameters).

### Changed

- **WebSocket performance** - Enhanced WebSocket route registration with pre-compiled metadata for better parameter handling.
- **Admin route improvements** - Updated admin route registration to support both trailing and non-trailing slash versions.

### Fixed

- **WebSocket connection management** - Enhanced resource management and stability in WebSocket handlers.

## [0.3.11]

### Added

- **WebSocket support** - Complete WebSocket implementation with FastAPI-like syntax using `@api.websocket()` decorator.
- **WebSocket testing** - `WebSocketTestClient` for testing WebSocket handlers without network.
- **WebSocket security** - Origin validation, authentication guards, and permission checks for WebSocket routes.
- **WebSocket configuration** - Configurable via Django settings: channel size, heartbeat interval, client timeout, allowed origins.
- **WebSocket documentation** - Comprehensive guide at `docs/WEBSOCKET.md`.

### Changed

- **Rust WebSocket infrastructure** - Actix-based WebSocket actor system with ASGI-style message queue bridge using tokio channels.
- **WebSocket routing** - Zero-overhead WebSocket route matching with support for path parameters.

### Fixed

- **WebSocket resource leak** - Fixed thread pool exhaustion by using `pyo3_async_runtimes::into_future()`.
- **WebSocket type coercion** - Fixed handling of `Annotated` types with PEP 563 string annotations.
- **WebSocket error handling** - Proper panic safety with `catch_unwind` and differentiated setup vs runtime errors.

## [0.3.10]

### Changed

- **Lazy user loading by default** - User loading now uses Django's `SimpleLazyObject` to defer database queries until `request.user` is first accessed. This avoids unnecessary DB calls when user data isn't needed.

## [0.3.9]

### Changed

- **Precompile optimizations** - Handler metadata (parameter extraction, validation, injectors) is now precompiled at route registration time instead of per-request. This eliminates repeated introspection overhead during request handling.

### Added

- **Static analysis for sync handlers** - New `analysis.py` module performs AST-based analysis of handler source code to detect Django ORM usage and blocking I/O patterns. Sync handlers without blocking calls can skip thread pool dispatch for better performance.

## [0.3.8]

### Fixed

- **Static file serving** - Fixed static route handler missing `is_async` and `injector` metadata, which caused static file routes to fail.

### Added

- **CLI `version` command** - Added `django-bolt version` command to display the installed version.
- **`llm.txt`** - Added LLM-friendly project summary file.

## [0.3.7]

### Fixed

- **CORS `@cors()` decorator validation** - The `@cors()` decorator now requires an explicit `origins` argument. Using `@cors()` without arguments previously created an empty CORS config that silently overrode global Django CORS settings, causing credentials and other headers to be missing. Now raises `ValueError` with helpful examples.
- **CORS for POST-only routes** - Routes that only had POST/PUT/PATCH methods (no GET) were not finding their CORS config during preflight, causing CORS failures.

### Changed

- **Shared CORS implementation** - Unified CORS handling between production server and test infrastructure. Test client now reads all CORS settings from Django (`CORS_ALLOWED_ORIGINS`, `CORS_ALLOW_CREDENTIALS`, `CORS_ALLOW_METHODS`, `CORS_ALLOW_HEADERS`, `CORS_EXPOSE_HEADERS`, `CORS_PREFLIGHT_MAX_AGE`).

## [0.3.6]

### Fixed

- **CORS preflight for non-existent routes** - OPTIONS preflight requests to non-existent routes now return 204 (success) instead of 404, allowing browsers to proceed with the actual request and display proper error messages.
- **CORS headers on 404 responses** - Non-existent routes now include CORS headers using global config, so browsers can read error responses.

### Changed

- Updated CORS documentation to emphasize Django settings-based configuration as the preferred approach.

## [0.3.5]

### Changed

- **Extended Serializer class** - Added more features like write_only, more built-in types to better work with django models.
- **Serializer Config class** - Renamed `Meta` to `Config` to avoid conflicts with `msgspec.Meta`.
- **Field configuration** - Removed direct Meta constraints from `field()` function; validation constraints now require `Annotated` and `Meta`.

### Fixed

- Fixed Python 3.14 annotation errors.

## [0.3.4]

### Added

- Python 3.14 support with msgspec 0.20.
- Advanced Serializer features including `kw_only` support.

### Changed

- Refactored concurrency handling in `sync_to_thread` function.
- Updated logging levels to DEBUG for improved debugging.

## [0.3.3]

### Added

- Docs changes related to serializer.

### Changed

- When None is returned from field validation function it uses the old value instead of setting it into None.

- dispatch function clean for performance.

### Fixed

## [0.3.2]

### Added

- `Serializer` class that extends msgspec struct using which we can validate response data using python function.

### Changed

- sync views are not handled by a thread not called directly in the dispatch function.

### Fixed

- Fixed Exception when orm query evaludated inside of the sync function.

- Fixed `response_model` not working.

## [0.3.1]

### Added

- **`request.user`** - Eager-loaded user objects for authenticated endpoints (eager-loaded at dispatch time)
- Type-safe dependency injection with runtime validation
- `preload_user` parameter to control user loading behavior (default: True for auth endpoints)
- New `user_loader.py` module for extensible user resolution
- Custom user model support via `get_user_model()`
- Override `get_user()` in auth backends for custom user resolution
- Authentication benchmarks for `/auth/me`, `/auth/me-dependency`, and `/auth/context` endpoints

### Changed

- Replaced `is_admin` with `is_superuser` (Django standard naming)
- Optimized Python request/response hot path
- Auth context type system improvements in `python/django_bolt/types.py`
- Guards module updated to use `is_superuser` instead of `is_admin`

### Fixed

-

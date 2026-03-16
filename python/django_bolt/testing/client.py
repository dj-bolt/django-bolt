"""Test clients for django-bolt using per-instance test state.

This version uses async-native Rust testing infrastructure which provides:
- Per-instance routers (no global state conflicts)
- Native async execution using Actix test utilities
- Production code path testing (same middleware, CORS, compression)
- Streaming response support via stream=True parameter
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
from collections.abc import Iterator
from typing import Any

import httpx
from httpx import Response

from django_bolt import BoltAPI, _core
from django_bolt.api import _validate_asgi_mount_conflicts

try:
    from django.conf import settings
except ImportError:
    settings = None  # type: ignore


class BoltTestTransport(httpx.BaseTransport):
    """HTTP transport that routes requests through django-bolt's test handler.

    Uses Actix's native test infrastructure which runs synchronously
    with an internal tokio runtime for proper request handling.

    Args:
        app_id: Test app instance ID
        raise_server_exceptions: If True, raise exceptions from handlers
    """

    def __init__(self, app_id: int, raise_server_exceptions: bool = True):
        self.app_id = app_id
        self.raise_server_exceptions = raise_server_exceptions

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request by routing it through Rust's Actix test infrastructure."""
        # Parse URL
        url = request.url
        path = url.path
        query_string = url.query.decode("utf-8") if url.query else None

        # Extract headers
        headers = [(k.decode("utf-8"), v.decode("utf-8")) for k, v in request.headers.raw]

        # Get body
        if hasattr(request, "_content"):
            body_bytes = request.content
        else:
            try:
                body_bytes = request.stream.read() if hasattr(request.stream, "read") else b"".join(request.stream)
            except Exception:
                body_bytes = request.content if hasattr(request, "_content") else b""

        method = request.method

        try:
            # Call the synchronous Rust test_request function
            # It creates its own tokio runtime internally for Actix test utilities
            status_code, resp_headers, resp_body = _core.test_request(
                app_id=self.app_id,
                method=method,
                path=path,
                headers=headers,
                body=body_bytes,
                query_string=query_string,
            )

            # Build httpx Response
            return Response(
                status_code=status_code,
                headers=resp_headers,
                content=resp_body,
                request=request,
            )

        except Exception as e:
            if self.raise_server_exceptions:
                raise
            # Return 500 error
            return Response(
                status_code=500,
                headers=[("content-type", "text/plain")],
                content=f"Test client error: {e}".encode(),
                request=request,
            )


class AsyncBoltTestTransport(httpx.AsyncBaseTransport):
    """Async HTTP transport that routes requests through django-bolt's test handler.

    Uses Actix's native test infrastructure. The underlying Rust function is
    synchronous (it creates its own tokio runtime), so we run it in a thread
    executor to avoid blocking the async event loop.

    Args:
        app_id: Test app instance ID
        raise_server_exceptions: If True, raise exceptions from handlers
    """

    def __init__(self, app_id: int, raise_server_exceptions: bool = True):
        self.app_id = app_id
        self.raise_server_exceptions = raise_server_exceptions

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request asynchronously through Rust's test infrastructure."""
        # Parse URL
        url = request.url
        path = url.path
        query_string = url.query.decode("utf-8") if url.query else None

        # Extract headers
        headers = [(k.decode("utf-8"), v.decode("utf-8")) for k, v in request.headers.raw]

        # Get body
        if hasattr(request, "_content"):
            body_bytes = request.content
        else:
            try:
                body_bytes = await request.aread()
            except Exception:
                body_bytes = b""

        method = request.method

        try:
            # Run the synchronous Rust function in a thread executor
            # to avoid blocking the async event loop
            loop = asyncio.get_running_loop()
            status_code, resp_headers, resp_body = await loop.run_in_executor(
                None,  # Use default executor
                lambda: _core.test_request(
                    app_id=self.app_id,
                    method=method,
                    path=path,
                    headers=headers,
                    body=body_bytes,
                    query_string=query_string,
                ),
            )

            # Build httpx Response
            return Response(
                status_code=status_code,
                headers=resp_headers,
                content=resp_body,
                request=request,
            )

        except Exception as e:
            if self.raise_server_exceptions:
                raise
            # Return 500 error
            return Response(
                status_code=500,
                headers=[("content-type", "text/plain")],
                content=f"Test client error: {e}".encode(),
                request=request,
            )


class TestClient(httpx.Client):
    """Synchronous test client for django-bolt using async-native Rust testing.

    This client:
    - Creates an isolated test app instance (no global state conflicts)
    - Routes through Actix test utilities (same as production)
    - Full middleware stack (CORS, rate limiting, compression)
    - Can run multiple tests in parallel without conflicts

    Usage:
        api = BoltAPI()

        @api.get("/hello")
        async def hello():
            return {"message": "world"}

        with TestClient(api) as client:
            response = client.get("/hello")
            assert response.status_code == 200
            assert response.json() == {"message": "world"}
    """

    __test__ = False  # Tell pytest this is not a test class

    @staticmethod
    def _read_cors_settings_from_django() -> dict | None:
        """Read all CORS settings from Django settings (same as production server).

        Returns:
            Dict with CORS config from Django settings, or None if not configured.
            Keys: origins, credentials, methods, headers, expose_headers, max_age
        """
        try:
            # Check if any CORS setting is defined
            has_origins = hasattr(settings, "CORS_ALLOWED_ORIGINS")
            has_all_origins = hasattr(settings, "CORS_ALLOW_ALL_ORIGINS") and settings.CORS_ALLOW_ALL_ORIGINS

            if not has_origins and not has_all_origins:
                return None

            # Build CORS config dict matching production server format
            cors_config = {}

            # Origins
            if has_all_origins:
                cors_config["origins"] = ["*"]
            elif has_origins:
                origins = settings.CORS_ALLOWED_ORIGINS
                if isinstance(origins, (list, tuple)):
                    cors_config["origins"] = list(origins)
                else:
                    cors_config["origins"] = []
            else:
                cors_config["origins"] = []

            # Credentials
            cors_config["credentials"] = getattr(settings, "CORS_ALLOW_CREDENTIALS", False)

            # Methods
            if hasattr(settings, "CORS_ALLOW_METHODS"):
                methods = settings.CORS_ALLOW_METHODS
                if isinstance(methods, (list, tuple)):
                    cors_config["methods"] = list(methods)

            # Headers
            if hasattr(settings, "CORS_ALLOW_HEADERS"):
                headers = settings.CORS_ALLOW_HEADERS
                if isinstance(headers, (list, tuple)):
                    cors_config["headers"] = list(headers)

            # Expose headers
            if hasattr(settings, "CORS_EXPOSE_HEADERS"):
                expose = settings.CORS_EXPOSE_HEADERS
                if isinstance(expose, (list, tuple)):
                    cors_config["expose_headers"] = list(expose)

            # Max age
            if hasattr(settings, "CORS_PREFLIGHT_MAX_AGE"):
                cors_config["max_age"] = settings.CORS_PREFLIGHT_MAX_AGE

            return cors_config
        except (ImportError, AttributeError):
            # Django not configured or settings not available
            return None

    @staticmethod
    def _read_static_files_settings_from_django() -> dict | None:
        """Read static files settings from Django settings.

        Returns:
            Dict with static files config, or None if not configured.
            Keys: url_prefix, directories, csp_header
        """
        try:
            if not hasattr(settings, "STATIC_URL") or not settings.STATIC_URL:
                return None

            url_prefix = settings.STATIC_URL.rstrip("/")
            if not url_prefix:
                return None

            directories = []

            # Get STATIC_ROOT
            if hasattr(settings, "STATIC_ROOT") and settings.STATIC_ROOT:
                static_root = str(settings.STATIC_ROOT)
                if static_root:
                    directories.append(static_root)

            # Get STATICFILES_DIRS
            if hasattr(settings, "STATICFILES_DIRS"):
                for d in settings.STATICFILES_DIRS:
                    dir_str = str(d)
                    if dir_str and dir_str not in directories:
                        directories.append(dir_str)

            if not directories:
                return None

            # Get CSP header if configured
            csp_header = None
            if hasattr(settings, "SECURE_CSP") and settings.SECURE_CSP:
                # Build CSP header string from directives
                csp_parts = []
                for directive, sources in settings.SECURE_CSP.items():
                    if sources:
                        filtered = [s for s in sources if "CSP_NONCE_SENTINEL" not in s]
                        if filtered:
                            csp_parts.append(f"{directive} {' '.join(filtered)}")
                    elif directive in ("upgrade-insecure-requests", "block-all-mixed-content"):
                        csp_parts.append(directive)
                if csp_parts:
                    csp_header = "; ".join(csp_parts)

            return {
                "url_prefix": url_prefix,
                "directories": directories,
                "csp_header": csp_header,
            }
        except (ImportError, AttributeError):
            return None

    @staticmethod
    def _validate_asgi_mount_conflicts(api: BoltAPI) -> None:
        """Validate exact-path conflicts for ASGI mounts (same rule as production startup)."""
        _validate_asgi_mount_conflicts(api._routes, getattr(api, "_asgi_mounts", []))

    def __init__(
        self,
        api: BoltAPI,
        base_url: str = "http://testserver.local",
        raise_server_exceptions: bool = True,
        cors_allowed_origins: list[str] | None = None,
        read_django_settings: bool = True,
        use_http_layer: bool = True,  # Ignored - kept for backward compatibility
        static_files_config: dict | None = None,
        **kwargs: Any,
    ):
        """Initialize test client.

        Args:
            api: BoltAPI instance to test
            base_url: Base URL for requests
            raise_server_exceptions: If True, raise exceptions from handlers
            cors_allowed_origins: Global CORS allowed origins for testing.
                                  If None and read_django_settings=True, reads from Django settings.
            read_django_settings: If True, read CORS settings from Django settings
                                 when cors_allowed_origins is None. Default True.
            use_http_layer: Ignored - all requests go through HTTP layer (Actix test utilities).
            static_files_config: Static files configuration dict with keys:
                                 url_prefix, directories, csp_header.
                                 If None and read_django_settings=True, reads from Django settings.
            **kwargs: Additional arguments passed to httpx.Client
        """
        # use_http_layer is ignored - we always use the HTTP layer now
        _ = use_http_layer

        # Build CORS config dict for Rust
        cors_config = None

        if cors_allowed_origins is not None:
            # Explicit origins provided - create minimal config
            cors_config = {"origins": cors_allowed_origins}
        elif read_django_settings:
            # Read full CORS config from Django settings (same as production server)
            cors_config = self._read_cors_settings_from_django()

        # Build static files config
        static_config = static_files_config
        if static_config is None and read_django_settings:
            static_config = self._read_static_files_settings_from_django()

        # Create test app instance with full CORS config
        # Pass trailing_slash setting to configure NormalizePath middleware
        trailing_slash = getattr(api, "trailing_slash", "strip")
        debug = getattr(settings, "DEBUG", False) if settings else False
        dispatch_sync = getattr(api, "_dispatch_sync", None)
        self.app_id = _core.create_test_app(api._dispatch, debug, cors_config, trailing_slash, static_config, dispatch_sync)

        # Validate mount collisions to mirror production startup behavior.
        self._validate_asgi_mount_conflicts(api)

        # Register routes
        rust_routes = [(method, path, handler_id, handler) for method, path, handler_id, handler in api._routes]
        _core.register_test_routes(self.app_id, rust_routes)

        # Register WebSocket routes with pre-compiled injectors (same as production)
        ws_routes = []
        for path, handler_id, handler in api._websocket_routes:
            # Get pre-compiled injector from handler metadata (same as runbolt.py)
            meta = api._handler_meta.get(handler_id, {})
            injector = meta.get("injector")
            ws_routes.append((path, handler_id, handler, injector))
        if ws_routes:
            _core.register_test_websocket_routes(self.app_id, ws_routes)

        # Register HTTP ASGI mounts
        if api._asgi_mounts:
            _core.register_test_asgi_mounts(self.app_id, list(api._asgi_mounts))

        # Register middleware metadata if any exists
        if api._handler_middleware:
            middleware_data = [(handler_id, meta) for handler_id, meta in api._handler_middleware.items()]
            _core.register_test_middleware_metadata(self.app_id, middleware_data)

        # Register authentication backends for user resolution (lazy loading in request.user)
        api._register_auth_backends()

        super().__init__(
            base_url=base_url,
            transport=BoltTestTransport(self.app_id, raise_server_exceptions),
            follow_redirects=True,
            **kwargs,
        )
        self.api = api

    def __enter__(self):
        """Enter context manager — runs lifespan startup if configured."""
        if self.api._has_lifespan:
            loop = asyncio.new_event_loop()
            cm = self.api._lifespan_context(self.api)
            try:
                loop.run_until_complete(cm.__aenter__())
            except BaseException:
                loop.close()
                raise
            self._lifespan_loop = loop
            self._lifespan_cm = cm
        try:
            return super().__enter__()
        except BaseException:
            if hasattr(self, "_lifespan_cm"):
                self._lifespan_loop.run_until_complete(
                    self._lifespan_cm.__aexit__(None, None, None)
                )
                self._lifespan_loop.close()
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager — runs lifespan shutdown, then cleans up test app."""
        try:
            if hasattr(self, "_lifespan_cm"):
                self._lifespan_loop.run_until_complete(
                    self._lifespan_cm.__aexit__(exc_type, exc_val, exc_tb)
                )
                self._lifespan_loop.close()
        finally:
            with contextlib.suppress(builtins.BaseException):
                _core.destroy_test_app(self.app_id)
        return super().__exit__(exc_type, exc_val, exc_tb)

    # Override HTTP methods to support stream=True
    def _add_streaming_methods(self, response: Response) -> Response:
        """Add iter_content() and iter_lines() methods to response."""
        response._iter_content = lambda chunk_size=1024, decode_unicode=False: self._iter_response_content(
            response.content, chunk_size, decode_unicode
        )
        response.iter_content = response._iter_content  # type: ignore

        response._iter_lines = lambda decode_unicode=True: self._iter_response_lines(response.content, decode_unicode)
        response.iter_lines = response._iter_lines  # type: ignore

        return response

    def get(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """GET request with optional streaming support."""
        response = super().get(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    def post(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """POST request with optional streaming support."""
        response = super().post(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    def put(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """PUT request with optional streaming support."""
        response = super().put(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    def patch(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """PATCH request with optional streaming support."""
        response = super().patch(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    def delete(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """DELETE request with optional streaming support."""
        response = super().delete(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    def head(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """HEAD request with optional streaming support."""
        response = super().head(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    def options(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """OPTIONS request with optional streaming support."""
        response = super().options(url, **kwargs)
        if stream:
            response = self._add_streaming_methods(response)
        return response

    @staticmethod
    def _iter_response_content(
        content: bytes, chunk_size: int = 1024, decode_unicode: bool = False
    ) -> Iterator[str | bytes]:
        """Iterate over response content in chunks.

        Args:
            content: Full response content
            chunk_size: Size of each chunk in bytes
            decode_unicode: If True, decode bytes to string using utf-8

        Yields:
            Chunks of response content
        """
        pos = 0
        while pos < len(content):
            chunk = content[pos : pos + chunk_size]
            pos += chunk_size

            if decode_unicode:
                yield chunk.decode("utf-8")
            else:
                yield chunk

    @staticmethod
    def _iter_response_lines(content: bytes, decode_unicode: bool = True) -> Iterator[str]:
        """Iterate over response content line by line.

        Args:
            content: Full response content
            decode_unicode: If True, decode bytes to string (default True)

        Yields:
            Lines from the response
        """
        buffer = b"" if not decode_unicode else ""

        for chunk in TestClient._iter_response_content(content, chunk_size=8192, decode_unicode=decode_unicode):
            if chunk:
                buffer += chunk

                # Split on newlines
                lines = buffer.split(b"\n") if isinstance(buffer, bytes) else buffer.split("\n")

                # Yield all complete lines, keep incomplete line in buffer
                for line in lines[:-1]:
                    yield line if isinstance(line, str) else line.decode("utf-8")

                buffer = lines[-1]

        # Yield any remaining data in buffer
        if buffer:
            yield buffer if isinstance(buffer, str) else buffer.decode("utf-8")


class AsyncTestClient(httpx.AsyncClient):
    """Async test client for django-bolt using async-native Rust testing.

    This client:
    - Creates an isolated test app instance (no global state conflicts)
    - Routes through Actix test utilities (same as production)
    - Full middleware stack (CORS, rate limiting, compression)
    - Native async/await support

    Usage:
        api = BoltAPI()

        @api.get("/hello")
        async def hello():
            return {"message": "world"}

        async with AsyncTestClient(api) as client:
            response = await client.get("/hello")
            assert response.status_code == 200
            assert response.json() == {"message": "world"}
    """

    __test__ = False  # Tell pytest this is not a test class

    def __init__(
        self,
        api: BoltAPI,
        base_url: str = "http://testserver.local",
        raise_server_exceptions: bool = True,
        cors_allowed_origins: list[str] | None = None,
        read_django_settings: bool = True,
        static_files_config: dict | None = None,
        **kwargs: Any,
    ):
        """Initialize async test client.

        Args:
            api: BoltAPI instance to test
            base_url: Base URL for requests
            raise_server_exceptions: If True, raise exceptions from handlers
            cors_allowed_origins: Global CORS allowed origins for testing.
            read_django_settings: If True, read CORS settings from Django settings.
            static_files_config: Static files configuration dict with keys:
                                 url_prefix, directories, csp_header.
                                 If None and read_django_settings=True, reads from Django settings.
            **kwargs: Additional arguments passed to httpx.AsyncClient
        """
        # Build CORS config dict for Rust
        cors_config = None

        if cors_allowed_origins is not None:
            cors_config = {"origins": cors_allowed_origins}
        elif read_django_settings:
            cors_config = TestClient._read_cors_settings_from_django()

        # Build static files config
        static_config = static_files_config
        if static_config is None and read_django_settings:
            static_config = TestClient._read_static_files_settings_from_django()

        # Create test app instance with trailing_slash setting
        trailing_slash = getattr(api, "trailing_slash", "strip")
        debug = getattr(settings, "DEBUG", False) if settings else False
        dispatch_sync = getattr(api, "_dispatch_sync", None)
        self.app_id = _core.create_test_app(api._dispatch, debug, cors_config, trailing_slash, static_config, dispatch_sync)

        # Validate mount collisions to mirror production startup behavior.
        TestClient._validate_asgi_mount_conflicts(api)

        # Register routes
        rust_routes = [(method, path, handler_id, handler) for method, path, handler_id, handler in api._routes]
        _core.register_test_routes(self.app_id, rust_routes)

        # Register WebSocket routes
        ws_routes = []
        for path, handler_id, handler in api._websocket_routes:
            meta = api._handler_meta.get(handler_id, {})
            injector = meta.get("injector")
            ws_routes.append((path, handler_id, handler, injector))
        if ws_routes:
            _core.register_test_websocket_routes(self.app_id, ws_routes)

        # Register HTTP ASGI mounts
        if api._asgi_mounts:
            _core.register_test_asgi_mounts(self.app_id, list(api._asgi_mounts))

        # Register middleware metadata
        if api._handler_middleware:
            middleware_data = [(handler_id, meta) for handler_id, meta in api._handler_middleware.items()]
            _core.register_test_middleware_metadata(self.app_id, middleware_data)

        api._register_auth_backends()

        super().__init__(
            base_url=base_url,
            transport=AsyncBoltTestTransport(self.app_id, raise_server_exceptions),
            follow_redirects=True,
            **kwargs,
        )
        self.api = api

    async def __aenter__(self):
        """Enter async context manager — runs lifespan startup if configured."""
        if self.api._has_lifespan:
            cm = self.api._lifespan_context(self.api)
            await cm.__aenter__()
            self._lifespan_cm = cm
        try:
            return await super().__aenter__()
        except BaseException:
            if hasattr(self, "_lifespan_cm"):
                await self._lifespan_cm.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager — runs lifespan shutdown, then cleans up test app."""
        try:
            if hasattr(self, "_lifespan_cm"):
                await self._lifespan_cm.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            with contextlib.suppress(builtins.BaseException):
                _core.destroy_test_app(self.app_id)
        return await super().__aexit__(exc_type, exc_val, exc_tb)

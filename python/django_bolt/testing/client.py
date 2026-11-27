"""Test clients for django-bolt using per-instance test state.

This version uses async-native Rust testing infrastructure which provides:
- Per-instance routers (no global state conflicts)
- Native async execution using Actix test utilities
- Production code path testing (same middleware, CORS, compression)
- Streaming response support via stream=True parameter
"""

from __future__ import annotations

import asyncio
import codecs
import dataclasses
import logging
import threading
import warnings
import weakref
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
from httpx import Response

from django_bolt import BoltAPI, _core, async_orm
from django_bolt._script_prefix import asgi_mounts, lifespan_entry, mcp_mounts, refuse_files_at_script_name
from django_bolt._script_prefix import configure as configure_script_prefix
from django_bolt._urlconf import served_api
from django_bolt.api import (
    _routes_and_native_routes,
    _validate_asgi_mount_conflicts,
    _validate_mcp_mount_conflicts,
    run_lifespans,
)
from django_bolt.middleware.middleware import refuse_unread_route_config
from django_bolt.testing.dbshare import SharedConnections
from django_bolt.testing.server_exceptions import ServerExceptions, bound_dispatches

logger = logging.getLogger(__name__)

try:
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured
    from django.db import connections
except ImportError:
    settings = None  # type: ignore
    ImproperlyConfigured = None  # type: ignore
    connections = None  # type: ignore


_prepare_api_lock = threading.Lock()


def _frozen(value: Any) -> Any:
    """Return a copy of ``value`` that a later change of a list or a dict in it does not change."""
    if isinstance(value, list | tuple):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, dict):
        return tuple((key, _frozen(item)) for key, item in value.items())
    if isinstance(value, set | frozenset):
        return frozenset(value)
    return value


def _config_key(config: Any) -> tuple[Any, ...] | None:
    """Return the values of an ``OpenAPIConfig``, to compare with the values of an earlier client."""
    if config is None:
        return None
    return tuple(_frozen(getattr(config, field.name)) for field in dataclasses.fields(config))


def _prepare_api(api: BoltAPI) -> list[tuple[Any, ...]]:
    """Add the routes that a test client makes on ``api``. Return the docs routes.

    Clients of one API can start in parallel threads. A client adds routes to
    the API, and another client reads the dicts of the API. Thus one client at
    a time does the additions, before it reads the API. The first client of an
    API adds the routes. A later client finds them, and changes nothing.
    """
    with _prepare_api_lock:
        # Rust answers a request with no route through this route.
        api._no_route_handler()
        docs_routes = _docs_routes(api)
        # A new client generates the schema again, so it has the routes added before it.
        api._openapi_schema = None
        return docs_routes


def _docs_routes(api: BoltAPI) -> list[tuple[Any, ...]]:
    """Return the docs routes of a test client of ``api``. Call it with ``_prepare_api_lock`` held.

    runbolt adds the docs routes to ``api._routes`` at startup, after each
    ``mount()``. A test client can come before a mount, and ``mount()`` copies
    ``api._routes``. Thus a client keeps its docs routes out of ``api._routes``.
    The registration occurs one time for each set of values of the ``OpenAPIConfig``.
    """
    if api._openapi_routes_registered:
        # runbolt, or the test, added the docs routes to api._routes.
        return []
    # The values of the config are the key, so a changed value registers again.
    key = _config_key(api._openapi_config)
    cached = api._client_docs_routes
    if cached is not None and cached[0] == key:
        return cached[1]
    start = len(api._routes)
    try:
        # The code of runbolt. A disabled config adds no route.
        api._register_openapi_routes()
        routes = api._routes[start:]
    finally:
        # Also when the registration raises, api._routes stays as it was.
        del api._routes[start:]
        api._openapi_routes_registered = False
    api._client_docs_routes = (key, routes)
    return routes


class BoltTestClientWarning(RuntimeWarning):
    """Advisory about a test setup that TestClient cannot serve correctly.

    It has its own category, thus you can stop it with
    ``warnings.simplefilter("ignore", BoltTestClientWarning)``.
    """


# Emitted at most once per process. The point is to explain a confusing
# failure mode once, not to annotate every call site: a suite that mixes
# database-free TestClient tests with a transactional marker would otherwise
# repeat this on every one of them. Test suites enter clients from several
# threads, so the check and the set have to be one atomic step.
_atomic_block_warning_emitted = threading.Event()
_atomic_block_warning_lock = threading.Lock()


def _warn_if_inside_atomic_block() -> None:
    """Give a warning when the test that enters the client holds a transaction.

    Requests go through the Rust pipeline, thus handlers run on framework
    threads that have their own database connections. A test that puts itself
    in a transaction (Django's ``TestCase``, or pytest-django's plain
    ``django_db``) does not commit. Thus the handler cannot read those rows.
    On SQLite the write lock of the test also stops the query of the handler,
    which gives ``database table is locked``. The cause is not clear from the
    symptom: a 500, or with ``raise_server_exceptions`` the lock error.

    The warning is advisory. A test whose handlers do not use the database is
    correct in a transaction, thus this function must not stop the run.

    The synchronous client is the only caller. In an event loop, Django gives a
    different connection object, thus the transaction of the test is not
    visible and there is nothing to detect.
    """
    if connections is None or _atomic_block_warning_emitted.is_set():
        return
    if settings is None or not settings.configured:
        return
    try:
        open_aliases = [conn.alias for conn in connections.all(initialized_only=True) if conn.in_atomic_block]
    except ImproperlyConfigured as exc:  # DATABASES missing or malformed
        logger.debug("Could not inspect database transaction state: %s", exc)
        return
    if not open_aliases:
        return
    with _atomic_block_warning_lock:
        if _atomic_block_warning_emitted.is_set():
            return
        _atomic_block_warning_emitted.set()
    warnings.warn(
        f"TestClient started in an open database transaction on: {', '.join(open_aliases)}. "
        "Handlers run on framework threads that have their own database connections. "
        "Thus they cannot read the rows that this test made, and on SQLite the lock of "
        "the test stops their queries. Remove share_db_connection=False, or use "
        "TransactionTestCase (or pytest-django's django_db(transaction=True)) for tests "
        "whose handlers use the database. "
        "See https://bolt.farhana.li/topics/testing/#using-djangos-test-runner",
        BoltTestClientWarning,
        stacklevel=3,
    )


class BoltTestTransport(httpx.BaseTransport):
    """HTTP transport that routes requests through django-bolt's test handler.

    Each request runs on a test worker thread, through the request pipeline
    of runbolt. The calling thread waits for the response.

    Args:
        app_id: Test app instance ID
        exceptions: The exceptions of the handlers, or None for raise_server_exceptions=False
        raise_server_exceptions: If True, raise the exception of a handler that gives a
            server error, and an error of the test backend. If False, give a 500 response
    """

    def __init__(self, app_id: int, exceptions: ServerExceptions | None = None, raise_server_exceptions: bool = True):
        self.app_id = app_id
        self.exceptions = exceptions
        self.raise_server_exceptions = raise_server_exceptions

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request by routing it through Rust's Actix test infrastructure."""
        # Parse URL
        url = request.url
        # Send the encoded path, as a real server receives it. Rust decodes path params.
        path = url.raw_path.split(b"?", 1)[0].decode("ascii")
        query_string = url.query.decode("utf-8") if url.query else None

        # Extract headers. An HTTP server removes the spaces and tabs around a value.
        headers = [(k.decode("utf-8"), v.decode("utf-8").strip(" \t")) for k, v in request.headers.raw]

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
            # The Rust function waits for the response of a test worker thread.
            status_code, resp_headers, resp_body = _core.test_request(
                app_id=self.app_id,
                method=method,
                path=path,
                headers=headers,
                body=body_bytes,
                query_string=query_string,
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

        if self.exceptions is not None and (error := self.exceptions.take(resp_headers)) is not None:
            raise error
        return Response(
            status_code=status_code,
            headers=resp_headers,
            content=resp_body,
            request=request,
        )


class AsyncBoltTestTransport(httpx.AsyncBaseTransport):
    """Async HTTP transport that routes requests through django-bolt's test handler.

    The Rust function waits for the response of a test worker thread. Thus
    it runs in a thread executor, so it does not block the event loop.

    Args:
        app_id: Test app instance ID
        exceptions: The exceptions of the handlers, or None for raise_server_exceptions=False
        raise_server_exceptions: If True, raise the exception of a handler that gives a
            server error, and an error of the test backend. If False, give a 500 response
    """

    def __init__(self, app_id: int, exceptions: ServerExceptions | None = None, raise_server_exceptions: bool = True):
        self.app_id = app_id
        self.exceptions = exceptions
        self.raise_server_exceptions = raise_server_exceptions

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request asynchronously through Rust's test infrastructure."""
        # Parse URL
        url = request.url
        # Send the encoded path, as a real server receives it. Rust decodes path params.
        path = url.raw_path.split(b"?", 1)[0].decode("ascii")
        query_string = url.query.decode("utf-8") if url.query else None

        # Extract headers. An HTTP server removes the spaces and tabs around a value.
        headers = [(k.decode("utf-8"), v.decode("utf-8").strip(" \t")) for k, v in request.headers.raw]

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

        if self.exceptions is not None and (error := self.exceptions.take(resp_headers)) is not None:
            raise error
        return Response(
            status_code=status_code,
            headers=resp_headers,
            content=resp_body,
            request=request,
        )


def _close_server_connections() -> None:
    """Close the database connections that the test server threads hold.

    A test client stands for a server, and a new server starts with no open
    connections. An open connection also blocks the drop of the test database
    at teardown. The lanes close theirs when they stop, and the test workers
    close theirs here. The event loops close theirs too (see async_orm.py).
    """
    _core.stop_idle_lanes()
    _core.close_test_worker_connections()
    async_orm.close_connections()


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
    def _validate_asgi_mount_conflicts(api: BoltAPI, docs_routes: list[tuple[Any, ...]]) -> None:
        """Validate exact-path conflicts for ASGI mounts (same rule as production startup)."""
        _validate_asgi_mount_conflicts(
            [*_routes_and_native_routes(api), *docs_routes], getattr(api, "_asgi_mounts", [])
        )

    @staticmethod
    def _validate_mcp_mount_conflicts(api: BoltAPI, docs_routes: list[tuple[Any, ...]]) -> None:
        """Validate MCP paths against routes and ASGI mounts, like production."""
        _validate_mcp_mount_conflicts(
            [*_routes_and_native_routes(api), *docs_routes],
            getattr(api, "_asgi_mounts", []),
            getattr(api, "_mcp_mounts", []),
        )

    @staticmethod
    def _mcp_mounts_for_test(api: BoltAPI, base_url: str) -> list[dict[str, Any]]:
        """Allow the test client's own Host when a mount uses secure defaults."""
        authority = urlsplit(base_url).netloc
        mounts: list[dict[str, Any]] = []
        for definition in api._mcp_mounts:
            if definition["allowed_hosts"] is None:
                definition = definition.copy()
                definition["allowed_hosts"] = [authority]
            mounts.append(definition)
        return mounts

    def __init__(
        self,
        api: BoltAPI,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        cors_allowed_origins: list[str] | None = None,
        read_django_settings: bool = True,
        use_http_layer: bool = True,  # Ignored - kept for backward compatibility
        static_files_config: dict | None = None,
        share_db_connection: bool = True,
        follow_redirects: bool = True,
        **kwargs: Any,
    ):
        """Initialize test client.

        Args:
            api: BoltAPI instance to test
            base_url: Base URL for requests
            raise_server_exceptions: If True (the default), raise the exception of a handler
                that gives a server error, as Starlette's TestClient does. An exception that
                the API answers with a 4xx, or an HTTPException, gives its response. Also
                raise an error of the test backend. If False, give the 500 response of the server
            cors_allowed_origins: Global CORS allowed origins for this test. They
                                  replace the CORS settings. A "*" entry allows every origin.
            read_django_settings: If True (the default), read the CORS, static and media
                                 settings as runbolt reads them. If False, use none of them.
            use_http_layer: Ignored - all requests go through HTTP layer (Actix test utilities).
            static_files_config: Static files configuration dict with keys:
                                 url_prefix, directories, csp_header, cache_control.
                                 It replaces the static settings.
            share_db_connection: If True (the default), handlers use the database
                                 connection of the test while the client is open, so
                                 plain ``TestCase`` and ``django_db`` see the rows of
                                 the test. Set False for a test that holds a cursor
                                 open across a request; then use ``TransactionTestCase``
                                 or ``django_db(transaction=True)``.
            follow_redirects: If True (the default), follow redirects. If False, give the
                             redirect response.
            **kwargs: Additional arguments passed to httpx.Client
        """
        # use_http_layer is ignored - we always use the HTTP layer now
        _ = use_http_layer

        # runbolt serves an API that the URLconf mounts under its prefixes.
        api = served_api(api)
        # runbolt registers the docs routes before it validates the routes. Do the same.
        docs_routes = _prepare_api(api)

        # Validate before allocating native test-app state. A failed constructor
        # has no client instance available to release that state. Route
        # conflicts and invalid capsules raise later, in register_test_routes;
        # the finalizer then releases the state.
        self._validate_asgi_mount_conflicts(api, docs_routes)
        self._validate_mcp_mount_conflicts(api, docs_routes)
        refuse_unread_route_config(api._routes, api._websocket_routes)

        # FORCE_SCRIPT_NAME: the entries from Rust into Python set the script prefix, as in runbolt.
        script_prefix = configure_script_prefix()
        if read_django_settings:
            refuse_files_at_script_name(script_prefix, static_files_replaced=static_files_config is not None)
        # Rust reads the Django settings with the function that runbolt uses.
        self.app_id = _core.create_test_app(
            api._dispatch,
            read_django_settings,
            cors_allowed_origins,
            static_files_config,
            api._rust_compression_config(),
            error_renderer=api._rust_error_renderer(script_prefix),
        )
        # Release the native app at close, or when nobody closed the client and
        # it is garbage. The native app holds the API, so a leak keeps it alive.
        self._release_app = weakref.finalize(self, _core.destroy_test_app, self.app_id)

        # Register routes
        # With raise_server_exceptions, a test-only dispatch brings the exception of a handler back.
        exceptions = ServerExceptions() if raise_server_exceptions else None
        rust_routes = [
            (method, path, handler_id, handler, *bound_dispatches(api, handler, handler_id, exceptions, script_prefix))
            for method, path, handler_id, handler in [*api._routes, *docs_routes]
        ]
        _core.register_test_routes(
            self.app_id, rust_routes, list(api._native_routes), api._rust_no_route(script_prefix)
        )

        # Register WebSocket routes with pre-compiled injectors (same as production)
        ws_routes = api._rust_websocket_routes(script_prefix)
        if ws_routes:
            _core.register_test_websocket_routes(self.app_id, ws_routes)

        # Register HTTP ASGI mounts
        if api._asgi_mounts:
            _core.register_test_asgi_mounts(self.app_id, asgi_mounts(api._asgi_mounts, script_prefix))

        # Register MCP mounts (served by the Rust rmcp core, like production)
        if api._mcp_mounts:
            _core.register_test_mcp_mounts(
                self.app_id, mcp_mounts(self._mcp_mounts_for_test(api, base_url), script_prefix)
            )

        # Register middleware metadata if any exists
        if api._handler_middleware:
            middleware_data = [(handler_id, meta) for handler_id, meta in api._handler_middleware.items()]
            _core.register_test_middleware_metadata(self.app_id, middleware_data)

        super().__init__(
            base_url=base_url,
            transport=BoltTestTransport(self.app_id, exceptions, raise_server_exceptions),
            follow_redirects=follow_redirects,
            **kwargs,
        )
        self.api = api
        self._script_prefix = script_prefix
        self._db_share = SharedConnections() if share_db_connection else None
        # The running lifespan: its loop and context manager. None before startup and after shutdown.
        self._lifespan: tuple[asyncio.AbstractEventLoop, Any] | None = None

    def __enter__(self):
        """Enter context manager — runs lifespan startup if configured."""
        if self.is_closed:
            # The test app is released. Do not run the lifespan of the API again.
            raise RuntimeError("TestClient is closed. Create a new TestClient for each `with` block.")
        if self._lifespan is not None:
            # A second startup would orphan the loop and the lifespan of the first one.
            raise RuntimeError("TestClient is already open. Enter it once.")
        if self._db_share is None or not self._db_share.install():
            _warn_if_inside_atomic_block()
        if self.api._has_lifespan:
            loop = asyncio.new_event_loop()
            cm = run_lifespans(self.api._source_lifespans)
            try:
                with lifespan_entry(self._script_prefix):
                    loop.run_until_complete(cm.__aenter__())
            except BaseException:
                loop.close()
                raise
            self._lifespan = (loop, cm)
        try:
            return super().__enter__()
        except BaseException:
            self._stop_lifespan(None, None, None)
            raise

    def _stop_lifespan(self, exc_type, exc_val, exc_tb) -> None:
        """Run the lifespan shutdown once, if the startup ran."""
        if self._lifespan is None:
            return
        loop, cm = self._lifespan
        self._lifespan = None
        try:
            with lifespan_entry(self._script_prefix):
                loop.run_until_complete(cm.__aexit__(exc_type, exc_val, exc_tb))
        finally:
            loop.close()

    def _shut_down(self, exc_type, exc_val, exc_tb) -> None:
        """Undo ``__enter__`` once: the lifespan, the shared connections, the native app.

        Each step runs also when an earlier one raises.
        """
        try:
            self._stop_lifespan(exc_type, exc_val, exc_tb)
        finally:
            try:
                if self._db_share is not None:
                    self._db_share.uninstall()
            finally:
                self._release_app()
                _close_server_connections()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager — runs lifespan shutdown, then cleans up test app."""
        try:
            self._shut_down(exc_type, exc_val, exc_tb)
        except BaseException:
            super().__exit__(exc_type, exc_val, exc_tb)
            raise
        return super().__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        """Close the client and release its native test app.

        A client entered without ``with`` undoes ``__enter__`` here.
        """
        try:
            self._shut_down(None, None, None)
        finally:
            super().close()

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

    def send(self, *args: Any, **kwargs: Any) -> Response:
        """Send one request, and report a shared-connection conflict as itself.

        A conflict is raised on a handler thread, thus the pipeline turns it
        into a plain 500. The cause belongs to the test that made the request.

        The locks of this thread are given back for the time of the request.
        See `SharedConnections.park`.
        """
        if self._db_share is None:
            return super().send(*args, **kwargs)
        # This thread waits in the Rust pipeline until the response arrives, thus
        # it runs no query. Holding its cursor locks would only block the handler.
        parked = self._db_share.park()
        try:
            response = super().send(*args, **kwargs)
        except BaseException:
            self._db_share.raise_if_conflict()
            raise
        finally:
            self._db_share.unpark(parked)
        self._db_share.raise_if_conflict()
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

    def query(self, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> Response:
        """QUERY request with optional streaming support."""
        response = self.request("QUERY", url, **kwargs)
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
        # A chunk can end inside a multi-byte character. The decoder keeps the part for the next chunk.
        decoder = codecs.getincrementaldecoder("utf-8")() if decode_unicode else None
        pos = 0
        while pos < len(content):
            chunk = content[pos : pos + chunk_size]
            pos += chunk_size

            if decoder is None:
                yield chunk
            else:
                text = decoder.decode(chunk, final=pos >= len(content))
                if text:
                    yield text

    @staticmethod
    def _iter_response_lines(content: bytes, decode_unicode: bool = True) -> Iterator[str | bytes]:
        """Iterate over response content line by line.

        Args:
            content: Full response content
            decode_unicode: If True, decode each line to a string (default True). Otherwise give bytes.

        Yields:
            Lines from the response
        """
        # Split the bytes, then decode each line. A chunk border can fall inside a character.
        lines = content.split(b"\n")
        if not lines[-1]:
            lines.pop()
        for line in lines:
            yield line.decode("utf-8") if decode_unicode else line


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
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        cors_allowed_origins: list[str] | None = None,
        read_django_settings: bool = True,
        static_files_config: dict | None = None,
        follow_redirects: bool = True,
        **kwargs: Any,
    ):
        """Initialize async test client.

        Args:
            api: BoltAPI instance to test
            base_url: Base URL for requests
            raise_server_exceptions: If True (the default), raise the exception of a handler
                that gives a server error, as Starlette's TestClient does. An exception that
                the API answers with a 4xx, or an HTTPException, gives its response. Also
                raise an error of the test backend. If False, give the 500 response of the server
            cors_allowed_origins: Global CORS allowed origins for this test. They
                                  replace the CORS settings. A "*" entry allows every origin.
            read_django_settings: If True (the default), read the CORS, static and media
                                 settings as runbolt reads them. If False, use none of them.
            static_files_config: Static files configuration dict with keys:
                                 url_prefix, directories, csp_header, cache_control.
                                 It replaces the static settings.
            follow_redirects: If True (the default), follow redirects. If False, give the
                             redirect response.
            **kwargs: Additional arguments passed to httpx.AsyncClient
        """
        api = served_api(api)
        # Register the docs routes as TestClient does.
        docs_routes = _prepare_api(api)

        # Validate before allocating native test-app state. A failed constructor
        # has no client instance available to release that state. Route
        # conflicts and invalid capsules raise later, in register_test_routes;
        # the finalizer then releases the state.
        TestClient._validate_asgi_mount_conflicts(api, docs_routes)
        TestClient._validate_mcp_mount_conflicts(api, docs_routes)
        refuse_unread_route_config(api._routes, api._websocket_routes)

        # FORCE_SCRIPT_NAME: the entries from Rust into Python set the script prefix, as in runbolt.
        script_prefix = configure_script_prefix()
        if read_django_settings:
            refuse_files_at_script_name(script_prefix, static_files_replaced=static_files_config is not None)
        # Rust reads the Django settings with the function that runbolt uses.
        self.app_id = _core.create_test_app(
            api._dispatch,
            read_django_settings,
            cors_allowed_origins,
            static_files_config,
            api._rust_compression_config(),
            error_renderer=api._rust_error_renderer(script_prefix),
        )
        # Release the native app at close, or when nobody closed the client and
        # it is garbage. The native app holds the API, so a leak keeps it alive.
        self._release_app = weakref.finalize(self, _core.destroy_test_app, self.app_id)

        # Register routes
        # With raise_server_exceptions, a test-only dispatch brings the exception of a handler back.
        exceptions = ServerExceptions() if raise_server_exceptions else None
        rust_routes = [
            (method, path, handler_id, handler, *bound_dispatches(api, handler, handler_id, exceptions, script_prefix))
            for method, path, handler_id, handler in [*api._routes, *docs_routes]
        ]
        _core.register_test_routes(
            self.app_id, rust_routes, list(api._native_routes), api._rust_no_route(script_prefix)
        )

        # Register WebSocket routes
        ws_routes = api._rust_websocket_routes(script_prefix)
        if ws_routes:
            _core.register_test_websocket_routes(self.app_id, ws_routes)

        # Register HTTP ASGI mounts
        if api._asgi_mounts:
            _core.register_test_asgi_mounts(self.app_id, asgi_mounts(api._asgi_mounts, script_prefix))

        # Register MCP mounts (served by the Rust rmcp core, like production)
        if api._mcp_mounts:
            _core.register_test_mcp_mounts(
                self.app_id, mcp_mounts(TestClient._mcp_mounts_for_test(api, base_url), script_prefix)
            )

        # Register middleware metadata
        if api._handler_middleware:
            middleware_data = [(handler_id, meta) for handler_id, meta in api._handler_middleware.items()]
            _core.register_test_middleware_metadata(self.app_id, middleware_data)

        super().__init__(
            base_url=base_url,
            transport=AsyncBoltTestTransport(self.app_id, exceptions, raise_server_exceptions),
            follow_redirects=follow_redirects,
            **kwargs,
        )
        self.api = api
        self._script_prefix = script_prefix
        # The context manager of the running lifespan. None before startup and after shutdown.
        self._lifespan: Any | None = None

    async def __aenter__(self):
        """Enter async context manager — runs lifespan startup if configured.

        There is no transaction advisory here, unlike ``TestClient.__enter__``.
        Django keeps connections in a thread-critical ``asgiref`` Local, so code
        that runs in an event loop gets its own connection object and cannot
        read the transaction that the test opened. The check would never fire.
        """
        if self.is_closed:
            # The test app is released. Do not run the lifespan of the API again.
            raise RuntimeError("AsyncTestClient is closed. Create a new AsyncTestClient for each `async with` block.")
        if self._lifespan is not None:
            raise RuntimeError("AsyncTestClient is already open. Enter it once.")
        if self.api._has_lifespan:
            cm = run_lifespans(self.api._source_lifespans)
            with lifespan_entry(self._script_prefix):
                await cm.__aenter__()
            self._lifespan = cm
        try:
            return await super().__aenter__()
        except BaseException:
            await self._stop_lifespan(None, None, None)
            raise

    async def _stop_lifespan(self, exc_type, exc_val, exc_tb) -> None:
        """Run the lifespan shutdown once, if the startup ran."""
        if self._lifespan is None:
            return
        cm = self._lifespan
        self._lifespan = None
        with lifespan_entry(self._script_prefix):
            await cm.__aexit__(exc_type, exc_val, exc_tb)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager — runs lifespan shutdown, then cleans up test app."""
        try:
            try:
                await self._stop_lifespan(exc_type, exc_val, exc_tb)
            finally:
                self._release_app()
                _close_server_connections()
        except BaseException:
            await super().__aexit__(exc_type, exc_val, exc_tb)
            raise
        return await super().__aexit__(exc_type, exc_val, exc_tb)

    async def aclose(self) -> None:
        """Close the client and release its native test app.

        A client entered without ``async with`` runs its lifespan shutdown here.
        """
        try:
            await self._stop_lifespan(None, None, None)
        finally:
            await super().aclose()
            self._release_app()
            _close_server_connections()

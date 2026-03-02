"""WebSocket testing utilities for django-bolt.

Provides test client for WebSocket endpoints without subprocess/network overhead.
Routes through Rust for path matching, authentication, and guard evaluation,
then uses mock ASGI interface for bidirectional message handling.

Usage:
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        async for msg in websocket.iter_text():
            await websocket.send_text(f"Echo: {msg}")

    async with WebSocketTestClient(api, "/ws/echo") as ws:
        await ws.send_text("hello")
        response = await ws.receive_text()
        assert response == "Echo: hello"
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from django_bolt import BoltAPI, _core
from django_bolt.websocket import CloseCode, WebSocket
from django_bolt.websocket.handlers import build_websocket_request, get_websocket_param_name

try:
    from django.conf import settings
except ImportError:
    settings = None  # type: ignore


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


class WebSocketTestClient:
    """Async WebSocket test client for django-bolt.

    This client routes through Rust for path matching, authentication, and
    guard evaluation (same as production), then uses mock ASGI receive/send
    for bidirectional message handling.

    Usage:
        async with WebSocketTestClient(api, "/ws/echo") as ws:
            await ws.send_text("hello")
            response = await ws.receive_text()
    """

    __test__ = False  # Tell pytest this is not a test class

    def __init__(
        self,
        api: BoltAPI,
        path: str,
        headers: dict[str, str] | None = None,
        query_string: str = "",
        subprotocols: list[str] | None = None,
        auth_context: Any = None,
        cors_allowed_origins: list[str] | None = None,
        read_django_settings: bool = True,
    ):
        """Initialize WebSocket test client.

        Args:
            api: BoltAPI instance with WebSocket routes
            path: WebSocket endpoint path (e.g., "/ws/echo")
            headers: Optional request headers
            query_string: Optional query string (without ?)
            subprotocols: Optional list of subprotocols to request
            auth_context: Optional authentication context for guard evaluation.
                         Should have user_id, is_superuser, is_staff, permissions attributes.
            cors_allowed_origins: Global CORS allowed origins for testing.
                                  If None and read_django_settings=True, reads from Django settings.
            read_django_settings: If True, read CORS settings from Django settings
                                 when cors_allowed_origins is None. Default True.
        """
        self.api = api
        self.path = path
        self.headers = headers or {}
        self.query_string = query_string
        self.subprotocols = subprotocols or []
        self.auth_context = auth_context

        # Build CORS config dict for Rust (same as HTTP TestClient)
        self._cors_config: dict | None = None
        if cors_allowed_origins is not None:
            # Explicit origins provided - create minimal config
            self._cors_config = {"origins": cors_allowed_origins}
        elif read_django_settings:
            # Read full CORS config from Django settings (same as production server)
            self._cors_config = _read_cors_settings_from_django()

        # Message queues for bidirectional communication
        self._client_to_server: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._server_to_client: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Connection state
        self._accepted = False
        self._closed = False
        self._close_code: int | None = None
        self._accepted_subprotocol: str | None = None
        self._handler_task: asyncio.Task | None = None
        self._handler_exception: Exception | None = None

        # Test app ID (set when used with TestClient context)
        self._app_id: int | None = None

    def _get_or_create_app_id(self) -> int:
        """Get or create a test app ID for Rust routing."""
        if self._app_id is not None:
            return self._app_id

        # Create a test app instance for this WebSocket test with CORS config
        # This ensures WebSocket origin validation uses same config as HTTP
        dispatch_sync = getattr(self.api, "_dispatch_sync", None)
        self._app_id = _core.create_test_app(self.api._dispatch, False, self._cors_config, None, None, dispatch_sync)

        # Register WebSocket routes with pre-compiled injectors (same as production)
        ws_routes = []
        for path, handler_id, handler in self.api._websocket_routes:
            # Get pre-compiled injector from handler metadata (same as runbolt.py)
            meta = self.api._handler_meta.get(handler_id, {})
            injector = meta.get("injector")
            ws_routes.append((path, handler_id, handler, injector))
        if ws_routes:
            _core.register_test_websocket_routes(self._app_id, ws_routes)

        # Register middleware metadata for guards/auth
        if self.api._handler_middleware:
            middleware_data = [(handler_id, meta) for handler_id, meta in self.api._handler_middleware.items()]
            _core.register_test_middleware_metadata(self._app_id, middleware_data)

        return self._app_id

    def _cleanup_app(self) -> None:
        """Cleanup test app instance."""
        if self._app_id is not None:
            with contextlib.suppress(Exception):
                _core.destroy_test_app(self._app_id)
            self._app_id = None

    def _find_handler_via_rust(self) -> tuple[bool, int, Callable, dict[str, Any], dict[str, Any]]:
        """Find WebSocket handler and build scope via Rust.

        Routes through Rust for path matching, auth, and guard evaluation.

        Returns:
            Tuple of (found, handler_id, handler, path_params, scope)

        Raises:
            ValueError: If no handler found for path
            PermissionError: If guards fail
        """
        app_id = self._get_or_create_app_id()

        # Build headers list for Rust
        headers_list = list(self.headers.items())

        try:
            found, handler_id, handler, path_params, scope = _core.handle_test_websocket(
                app_id,
                self.path,
                headers_list,
                self.query_string if self.query_string else None,
            )
        except PermissionError as e:
            # Re-raise permission errors from Rust
            raise PermissionError(f"WebSocket connection denied: {e}") from e

        if not found:
            raise ValueError(f"No WebSocket handler found for path: {self.path}")

        # Convert path_params from Rust dict to Python dict
        path_params_dict = dict(path_params) if path_params else {}

        # Convert scope from Rust dict to Python dict and add extras
        scope_dict = dict(scope) if scope else {}
        scope_dict["subprotocols"] = self.subprotocols

        # Add auth context if provided (for Python-side guard evaluation fallback)
        if self.auth_context is not None:
            scope_dict["auth_context"] = self.auth_context

        return found, handler_id, handler, path_params_dict, scope_dict

    async def _receive(self) -> dict[str, Any]:
        """ASGI receive callable - gets messages from client queue."""
        return await self._client_to_server.get()

    async def _send(self, message: dict[str, Any]) -> None:
        """ASGI send callable - puts messages to server queue."""
        msg_type = message.get("type", "")

        if msg_type == "websocket.accept":
            self._accepted = True
            self._accepted_subprotocol = message.get("subprotocol")

        elif msg_type == "websocket.close":
            self._closed = True
            self._close_code = message.get("code", CloseCode.NORMAL)

        # Put all messages in queue for client to receive
        await self._server_to_client.put(message)

    async def __aenter__(self) -> WebSocketTestClient:
        """Enter async context - start the WebSocket handler.

        Routes through Rust for path matching, authentication, and guard evaluation.
        Uses the same production code path as Rust for parameter injection.
        """
        # Use Rust for path matching, auth, and guard evaluation
        _found, handler_id, handler, path_params, scope = self._find_handler_via_rust()

        # Create WebSocket instance
        ws = WebSocket(scope, self._receive, self._send)

        # Use production code path for parameter injection
        # This ensures tests actually verify the injector implementation

        # Get the pre-compiled injector from handler metadata (same as Rust does)
        meta = self.api._handler_meta.get(handler_id)

        if meta and "injector" in meta:
            # Build request dict from scope (same as Rust's build_websocket_request call)
            request = build_websocket_request(scope)

            # Call the pre-compiled injector to get (args, kwargs)
            injector = meta["injector"]
            if meta.get("injector_is_async", False):
                args, kwargs = await injector(request)
            else:
                args, kwargs = injector(request)

            # Prepend websocket to args (same as Rust does)
            args = [ws] + list(args)
        else:
            # Fallback for handlers without injector (simple websocket-only handlers)
            ws_param_name = get_websocket_param_name(handler)
            args = [ws] if ws_param_name else []
            kwargs = {}

        # Start handler in background task
        async def run_handler():
            try:
                await handler(*args, **kwargs)
            except Exception as e:
                self._handler_exception = e
                # Send disconnect on error
                if not self._closed:
                    await self._server_to_client.put(
                        {
                            "type": "websocket.close",
                            "code": CloseCode.INTERNAL_ERROR,
                        }
                    )
                    self._closed = True
                    self._close_code = CloseCode.INTERNAL_ERROR

        self._handler_task = asyncio.create_task(run_handler())

        # Give handler a chance to start
        await asyncio.sleep(0)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context - close connection and cleanup."""
        if not self._closed:
            # Send disconnect to handler
            await self._client_to_server.put(
                {
                    "type": "websocket.disconnect",
                    "code": CloseCode.NORMAL,
                }
            )
            self._closed = True

        # Cancel handler task if still running
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._handler_task

        # Cleanup test app instance
        self._cleanup_app()

        # Re-raise handler exception if any
        if self._handler_exception and exc_type is None:
            raise self._handler_exception

    @property
    def accepted(self) -> bool:
        """Whether the connection has been accepted."""
        return self._accepted

    @property
    def closed(self) -> bool:
        """Whether the connection has been closed."""
        return self._closed

    @property
    def close_code(self) -> int | None:
        """Close code if connection was closed."""
        return self._close_code

    @property
    def accepted_subprotocol(self) -> str | None:
        """Subprotocol accepted by server."""
        return self._accepted_subprotocol

    async def send_text(self, data: str) -> None:
        """Send a text message to the server."""
        if self._closed:
            raise RuntimeError("WebSocket is closed")
        await self._client_to_server.put(
            {
                "type": "websocket.receive",
                "text": data,
            }
        )
        # Give handler time to process
        await asyncio.sleep(0)

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary message to the server."""
        if self._closed:
            raise RuntimeError("WebSocket is closed")
        await self._client_to_server.put(
            {
                "type": "websocket.receive",
                "bytes": data,
            }
        )
        await asyncio.sleep(0)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        """Send JSON data to the server."""
        text = json.dumps(data, separators=(",", ":"))
        if mode == "text":
            await self.send_text(text)
        else:
            await self.send_bytes(text.encode("utf-8"))

    async def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        """Receive a raw message from the server."""
        try:
            return await asyncio.wait_for(self._server_to_client.get(), timeout=timeout)
        except TimeoutError as e:
            raise TimeoutError(f"No message received within {timeout}s") from e

    async def receive_text(self, timeout: float = 5.0) -> str:
        """Receive a text message from the server."""
        while True:
            msg = await self.receive(timeout=timeout)
            msg_type = msg.get("type", "")

            if msg_type == "websocket.send":
                if "text" in msg:
                    return msg["text"]
                elif "bytes" in msg:
                    return msg["bytes"].decode("utf-8")
            elif msg_type == "websocket.close":
                self._closed = True
                self._close_code = msg.get("code", CloseCode.NORMAL)
                raise ConnectionClosed(self._close_code)
            elif msg_type == "websocket.accept":
                # Skip accept messages
                continue
            else:
                # Unknown message type, skip
                continue

    async def receive_bytes(self, timeout: float = 5.0) -> bytes:
        """Receive a binary message from the server."""
        while True:
            msg = await self.receive(timeout=timeout)
            msg_type = msg.get("type", "")

            if msg_type == "websocket.send":
                if "bytes" in msg:
                    return msg["bytes"]
                elif "text" in msg:
                    return msg["text"].encode("utf-8")
            elif msg_type == "websocket.close":
                self._closed = True
                self._close_code = msg.get("code", CloseCode.NORMAL)
                raise ConnectionClosed(self._close_code)
            elif msg_type == "websocket.accept":
                continue
            else:
                continue

    async def receive_json(self, timeout: float = 5.0, mode: str = "text") -> Any:
        """Receive and parse JSON from the server."""
        if mode == "text":
            data = await self.receive_text(timeout=timeout)
        else:
            data = await self.receive_bytes(timeout=timeout)
            data = data.decode("utf-8")
        return json.loads(data)

    async def close(self, code: int = CloseCode.NORMAL) -> None:
        """Close the WebSocket connection."""
        if not self._closed:
            await self._client_to_server.put(
                {
                    "type": "websocket.disconnect",
                    "code": code,
                }
            )
            self._closed = True
            self._close_code = code

    async def iter_text(self, timeout: float = 5.0) -> AsyncIterator[str]:
        """Async iterator for text messages."""
        while not self._closed:
            try:
                yield await self.receive_text(timeout=timeout)
            except (ConnectionClosed, TimeoutError):
                break

    async def iter_bytes(self, timeout: float = 5.0) -> AsyncIterator[bytes]:
        """Async iterator for binary messages."""
        while not self._closed:
            try:
                yield await self.receive_bytes(timeout=timeout)
            except (ConnectionClosed, TimeoutError):
                break

    async def iter_json(self, timeout: float = 5.0) -> AsyncIterator[Any]:
        """Async iterator for JSON messages."""
        while not self._closed:
            try:
                yield await self.receive_json(timeout=timeout)
            except (ConnectionClosed, TimeoutError):
                break


class ConnectionClosed(Exception):
    """Raised when WebSocket connection is closed."""

    def __init__(self, code: int = CloseCode.NORMAL, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket closed with code {code}: {reason}")


# For backwards compatibility and convenience
WebSocketClient = WebSocketTestClient

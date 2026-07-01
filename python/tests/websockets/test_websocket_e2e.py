"""
Comprehensive end-to-end WebSocket tests using WebSocketTestClient.
Tests the full WebSocket lifecycle with actual handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Annotated

import django
import jwt
import pytest
from django.conf import settings

# Configure Django before imports
if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-secret-key-for-websocket-e2e",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django_bolt",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        USE_TZ=True,
    )
    django.setup()

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import HasPermission, IsAdminUser, IsAuthenticated, JWTAuthentication
from django_bolt.testing import ConnectionClosed, WebSocketTestClient
from django_bolt.websocket import CloseCode

# Test secret for JWT tokens
E2E_JWT_SECRET = "test-secret-key-for-websocket-e2e-jwt"


def create_e2e_jwt(
    user_id: int = 123,
    is_staff: bool = False,
    is_superuser: bool = False,
    permissions: list[str] | None = None,
) -> str:
    """Create a JWT token for e2e testing."""
    payload = {
        "sub": str(user_id),
        "user_id": str(user_id),
        "is_staff": is_staff,
        "is_superuser": is_superuser,
        "permissions": permissions or [],
        "exp": int(time.time()) + 3600,  # 1 hour from now
        "iat": int(time.time()),
    }
    return jwt.encode(payload, E2E_JWT_SECRET, algorithm="HS256")


class TestWebSocketEcho:
    """Test basic echo functionality."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/echo")
        async def echo_handler(websocket: WebSocket):
            await websocket.accept()
            with contextlib.suppress(Exception):
                async for message in websocket.iter_text():
                    await websocket.send_text(f"Echo: {message}")

        return api

    @pytest.mark.asyncio
    async def test_echo_text(self, api):
        """Test basic text echo."""
        async with WebSocketTestClient(api, "/ws/echo") as ws:
            await ws.send_text("hello")
            response = await ws.receive_text()
            assert response == "Echo: hello"

    @pytest.mark.asyncio
    async def test_echo_multiple_messages(self, api):
        """Test multiple messages in sequence."""
        async with WebSocketTestClient(api, "/ws/echo") as ws:
            for i in range(5):
                await ws.send_text(f"message {i}")
                response = await ws.receive_text()
                assert response == f"Echo: message {i}"

    @pytest.mark.asyncio
    async def test_echo_empty_string(self, api):
        """Test echoing empty string."""
        async with WebSocketTestClient(api, "/ws/echo") as ws:
            await ws.send_text("")
            response = await ws.receive_text()
            assert response == "Echo: "

    @pytest.mark.asyncio
    async def test_echo_unicode(self, api):
        """Test echoing unicode characters."""
        async with WebSocketTestClient(api, "/ws/echo") as ws:
            await ws.send_text("Hello 世界 🌍")
            response: str = await ws.receive_text()
            assert response == "Echo: Hello 世界 🌍"


class TestWebSocketJSON:
    """Test JSON message handling."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/json")
        async def json_handler(websocket: WebSocket):
            await websocket.accept()
            with contextlib.suppress(Exception):
                async for data in websocket.iter_json():
                    # Echo back with processing
                    response = {
                        "received": data,
                        "type": type(data).__name__,
                    }
                    await websocket.send_json(response)

        return api

    @pytest.mark.asyncio
    async def test_json_dict(self, api):
        """Test sending and receiving JSON dict."""
        async with WebSocketTestClient(api, "/ws/json") as ws:
            await ws.send_json({"name": "test", "value": 42})
            response = await ws.receive_json()
            assert response["received"] == {"name": "test", "value": 42}
            assert response["type"] == "dict"

    @pytest.mark.asyncio
    async def test_json_list(self, api):
        """Test sending and receiving JSON list."""
        async with WebSocketTestClient(api, "/ws/json") as ws:
            await ws.send_json([1, 2, 3, "four"])
            response = await ws.receive_json()
            assert response["received"] == [1, 2, 3, "four"]
            assert response["type"] == "list"

    @pytest.mark.asyncio
    async def test_json_nested(self, api):
        """Test sending nested JSON structures."""
        async with WebSocketTestClient(api, "/ws/json") as ws:
            data = {
                "users": [
                    {"name": "Alice", "age": 30},
                    {"name": "Bob", "age": 25},
                ],
                "meta": {"count": 2, "page": 1},
            }
            await ws.send_json(data)
            response = await ws.receive_json()
            assert response["received"] == data


class TestWebSocketBinary:
    """Test binary message handling."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/binary")
        async def binary_handler(websocket: WebSocket):
            await websocket.accept()
            with contextlib.suppress(Exception):
                async for data in websocket.iter_bytes():
                    # Echo back with length prefix
                    await websocket.send_bytes(len(data).to_bytes(4, "big") + data)

        return api

    @pytest.mark.asyncio
    async def test_binary_echo(self, api):
        """Test binary message echo."""
        async with WebSocketTestClient(api, "/ws/binary") as ws:
            data = b"\x00\x01\x02\x03\x04"
            await ws.send_bytes(data)
            response = await ws.receive_bytes()
            length = int.from_bytes(response[:4], "big")
            assert length == len(data)
            assert response[4:] == data

    @pytest.mark.asyncio
    async def test_binary_large(self, api):
        """Test larger binary message."""
        async with WebSocketTestClient(api, "/ws/binary") as ws:
            data = bytes(range(256)) * 100  # 25.6KB
            await ws.send_bytes(data)
            response = await ws.receive_bytes()
            length = int.from_bytes(response[:4], "big")
            assert length == len(data)
            assert response[4:] == data


class TestWebSocketPathParams:
    """Test WebSocket with path parameters."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/chat/{room_id}")
        async def chat_handler(websocket: WebSocket, room_id: str):
            await websocket.accept()
            await websocket.send_text(f"Joined room: {room_id}")
            with contextlib.suppress(Exception):
                async for message in websocket.iter_text():
                    await websocket.send_text(f"[{room_id}] {message}")

        @api.websocket("/ws/user/{user_id}/channel/{channel_id}")
        async def multi_param_handler(websocket: WebSocket, user_id: str, channel_id: str):
            await websocket.accept()
            await websocket.send_json(
                {
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "status": "connected",
                }
            )

        return api

    @pytest.mark.asyncio
    async def test_single_path_param(self, api):
        """Test WebSocket with single path parameter."""
        async with WebSocketTestClient(api, "/ws/chat/general") as ws:
            welcome = await ws.receive_text()
            assert welcome == "Joined room: general"

            await ws.send_text("hello everyone")
            response = await ws.receive_text()
            assert response == "[general] hello everyone"

    @pytest.mark.asyncio
    async def test_different_room(self, api):
        """Test connecting to different rooms."""
        async with WebSocketTestClient(api, "/ws/chat/private-123") as ws:
            welcome = await ws.receive_text()
            assert welcome == "Joined room: private-123"

    @pytest.mark.asyncio
    async def test_multiple_path_params(self, api):
        """Test WebSocket with multiple path parameters."""
        async with WebSocketTestClient(api, "/ws/user/42/channel/tech") as ws:
            response = await ws.receive_json()
            assert response == {
                "user_id": "42",
                "channel_id": "tech",
                "status": "connected",
            }


class TestWebSocketQueryParams:
    """Test WebSocket with query parameters."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/auth")
        async def auth_handler(websocket: WebSocket):
            await websocket.accept()
            # Access query params from scope
            token = websocket.query_params.get("token", "none")
            await websocket.send_json({"authenticated": token != "none", "token": token})

        return api

    @pytest.mark.asyncio
    async def test_query_param(self, api):
        """Test accessing query parameters."""
        async with WebSocketTestClient(api, "/ws/auth", query_string="token=secret123") as ws:
            response = await ws.receive_json()
            assert response["authenticated"] is True
            assert response["token"] == "secret123"

    @pytest.mark.asyncio
    async def test_no_query_param(self, api):
        """Test without query parameters."""
        async with WebSocketTestClient(api, "/ws/auth") as ws:
            response = await ws.receive_json()
            assert response["authenticated"] is False
            assert response["token"] == "none"


class TestWebSocketHeaders:
    """Test WebSocket with custom headers."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/headers")
        async def headers_handler(websocket: WebSocket):
            await websocket.accept()
            # Access headers from scope
            user_agent = websocket.headers.get("user-agent", "unknown")
            custom = websocket.headers.get("x-custom-header", "none")
            await websocket.send_json(
                {
                    "user_agent": user_agent,
                    "custom_header": custom,
                }
            )

        return api

    @pytest.mark.asyncio
    async def test_custom_headers(self, api):
        """Test accessing custom headers."""
        headers = {
            "User-Agent": "TestClient/1.0",
            "X-Custom-Header": "custom-value",
        }
        async with WebSocketTestClient(api, "/ws/headers", headers=headers) as ws:
            response = await ws.receive_json()
            assert response["user_agent"] == "TestClient/1.0"
            assert response["custom_header"] == "custom-value"


class TestWebSocketClose:
    """Test WebSocket close handling."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/close-after")
        async def close_after_handler(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close(code=CloseCode.NORMAL, reason="done")

        @api.websocket("/ws/close-error")
        async def close_error_handler(websocket: WebSocket):
            await websocket.accept()
            await websocket.close(code=CloseCode.INTERNAL_ERROR, reason="something went wrong")

        return api

    @pytest.mark.asyncio
    async def test_server_close_normal(self, api):
        """Test server-initiated normal close."""
        async with WebSocketTestClient(api, "/ws/close-after") as ws:
            msg = await ws.receive_text()
            assert msg == "hello"

            # Next receive should raise ConnectionClosed
            with pytest.raises(ConnectionClosed) as exc_info:
                await ws.receive_text()

            assert exc_info.value.code == CloseCode.NORMAL

    @pytest.mark.asyncio
    async def test_server_close_error(self, api):
        """Test server-initiated error close."""
        async with WebSocketTestClient(api, "/ws/close-error") as ws:
            with pytest.raises(ConnectionClosed) as exc_info:
                await ws.receive_text()

            assert exc_info.value.code == CloseCode.INTERNAL_ERROR


class TestWebSocketConcurrency:
    """Test concurrent WebSocket connections."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()
        connection_count = {"value": 0}

        @api.websocket("/ws/counter")
        async def counter_handler(websocket: WebSocket):
            await websocket.accept()
            connection_count["value"] += 1
            my_count = connection_count["value"]
            await websocket.send_json({"connection_number": my_count})
            try:
                with contextlib.suppress(Exception):
                    await websocket.receive_text()  # Wait for close
            finally:
                connection_count["value"] -= 1

        return api

    @pytest.mark.asyncio
    async def test_multiple_connections(self, api):
        """Test multiple concurrent connections."""
        async with WebSocketTestClient(api, "/ws/counter") as ws1:
            response1 = await ws1.receive_json()
            assert response1["connection_number"] == 1

            async with WebSocketTestClient(api, "/ws/counter") as ws2:
                response2 = await ws2.receive_json()
                assert response2["connection_number"] == 2


class TestWebSocketErrors:
    """Test WebSocket error handling."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/error")
        async def error_handler(websocket: WebSocket):
            await websocket.accept()
            raise ValueError("Test error")

        return api

    @pytest.mark.asyncio
    async def test_handler_error(self, api):
        """Test handler error handling."""
        with pytest.raises(ValueError) as exc_info:
            async with WebSocketTestClient(api, "/ws/error"):
                await asyncio.sleep(0.1)  # Give handler time to error

        assert "Test error" in str(exc_info.value)


class TestWebSocketTimeout:
    """Test WebSocket timeout handling."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/slow")
        async def slow_handler(websocket: WebSocket):
            await websocket.accept()
            # Don't send anything - client should timeout

        return api

    @pytest.mark.asyncio
    async def test_receive_timeout(self, api):
        """Test receive timeout."""
        async with WebSocketTestClient(api, "/ws/slow") as ws:
            with pytest.raises(TimeoutError):
                await ws.receive_text(timeout=0.1)


class TestWebSocketIterators:
    """Test WebSocket async iterators."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/broadcast")
        async def broadcast_handler(websocket: WebSocket):
            await websocket.accept()
            # Wait for client to send a "start" signal
            msg = await websocket.receive_text()
            if msg == "start":
                for i in range(3):
                    await websocket.send_text(f"message {i}")
                await websocket.close()

        return api

    @pytest.mark.asyncio
    async def test_iter_text(self, api):
        """Test receiving multiple messages."""
        messages = []
        async with WebSocketTestClient(api, "/ws/broadcast") as ws:
            # Send start signal
            await ws.send_text("start")
            # Give handler time to process and send messages
            await asyncio.sleep(0.1)
            # Receive all 3 messages
            for _ in range(3):
                try:
                    msg = await ws.receive_text(timeout=1.0)
                    messages.append(msg)
                except (ConnectionClosed, TimeoutError):
                    break

        assert messages == ["message 0", "message 1", "message 2"]


class TestWebSocketState:
    """Test WebSocket connection state tracking."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/state")
        async def state_handler(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("ready")
            with contextlib.suppress(Exception):
                await websocket.receive_text()

        return api

    @pytest.mark.asyncio
    async def test_accepted_state(self, api):
        """Test accepted state tracking."""
        async with WebSocketTestClient(api, "/ws/state") as ws:
            # Wait for accept
            await ws.receive_text()
            assert ws.accepted is True
            assert ws.closed is False

    @pytest.mark.asyncio
    async def test_closed_state(self, api):
        """Test closed state tracking."""
        ws = WebSocketTestClient(api, "/ws/state")
        async with ws:
            await ws.receive_text()
            assert ws.closed is False

        assert ws.closed is True


class TestWebSocketGuards:
    """Test WebSocket guard/auth integration with real JWT authentication."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/public")
        async def public_ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("public")

        @api.websocket(
            "/ws/protected",
            auth=[JWTAuthentication(secret=E2E_JWT_SECRET)],
            guards=[IsAuthenticated()],
        )
        async def protected_ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("protected")

        @api.websocket(
            "/ws/admin",
            auth=[JWTAuthentication(secret=E2E_JWT_SECRET)],
            guards=[IsAdminUser()],
        )
        async def admin_ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("admin")

        @api.websocket(
            "/ws/permission",
            auth=[JWTAuthentication(secret=E2E_JWT_SECRET)],
            guards=[HasPermission("api.view_data")],
        )
        async def permission_ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("has permission")

        return api

    @pytest.mark.asyncio
    async def test_public_route_no_auth(self, api):
        """Test public route works without auth."""
        async with WebSocketTestClient(api, "/ws/public") as ws:
            msg = await ws.receive_text()
            assert msg == "public"

    @pytest.mark.asyncio
    async def test_protected_route_without_auth(self, api):
        """Test protected route fails without JWT token."""
        with pytest.raises(PermissionError) as exc_info:
            async with WebSocketTestClient(api, "/ws/protected"):
                pass

        assert "Authentication required" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_protected_route_with_auth(self, api):
        """Test protected route works with valid JWT token."""
        token = create_e2e_jwt(user_id=123)
        headers = {"Authorization": f"Bearer {token}"}

        async with WebSocketTestClient(api, "/ws/protected", headers=headers) as ws:
            msg = await ws.receive_text()
            assert msg == "protected"

    @pytest.mark.asyncio
    async def test_admin_route_without_superuser(self, api):
        """Test admin route fails for non-superuser."""
        token = create_e2e_jwt(user_id=123, is_superuser=False)
        headers = {"Authorization": f"Bearer {token}"}

        with pytest.raises(PermissionError) as exc_info:
            async with WebSocketTestClient(api, "/ws/admin", headers=headers):
                pass

        assert "Permission denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_admin_route_with_superuser(self, api):
        """Test admin route works for superuser."""
        token = create_e2e_jwt(user_id=123, is_superuser=True, is_staff=True)
        headers = {"Authorization": f"Bearer {token}"}

        async with WebSocketTestClient(api, "/ws/admin", headers=headers) as ws:
            msg = await ws.receive_text()
            assert msg == "admin"

    @pytest.mark.asyncio
    async def test_permission_route_without_permission(self, api):
        """Test permission-guarded route fails without required permission."""
        token = create_e2e_jwt(user_id=123, permissions=[])
        headers = {"Authorization": f"Bearer {token}"}

        with pytest.raises(PermissionError) as exc_info:
            async with WebSocketTestClient(api, "/ws/permission", headers=headers):
                pass

        assert "Permission denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_permission_route_with_permission(self, api):
        """Test permission-guarded route works with required permission."""
        token = create_e2e_jwt(user_id=123, permissions=["api.view_data", "api.edit_data"])
        headers = {"Authorization": f"Bearer {token}"}

        async with WebSocketTestClient(api, "/ws/permission", headers=headers) as ws:
            msg = await ws.receive_text()
            assert msg == "has permission"


class TestWebSocketTypeCoercion:
    """Test WebSocket path parameter type coercion."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/user/{user_id}")
        async def user_handler(websocket: WebSocket, user_id: int):
            await websocket.accept()
            # Verify type coercion worked
            await websocket.send_json(
                {
                    "user_id": user_id,
                    "type": type(user_id).__name__,
                }
            )

        @api.websocket("/ws/price/{price}")
        async def price_handler(websocket: WebSocket, price: float):
            await websocket.accept()
            await websocket.send_json(
                {
                    "price": price,
                    "type": type(price).__name__,
                }
            )

        @api.websocket("/ws/active/{active}")
        async def active_handler(websocket: WebSocket, active: bool):
            await websocket.accept()
            await websocket.send_json(
                {
                    "active": active,
                    "type": type(active).__name__,
                }
            )

        return api

    @pytest.mark.asyncio
    async def test_int_coercion(self, api):
        """Test integer path parameter coercion."""
        async with WebSocketTestClient(api, "/ws/user/42") as ws:
            response = await ws.receive_json()
            assert response["user_id"] == 42
            assert response["type"] == "int"

    @pytest.mark.asyncio
    async def test_float_coercion(self, api):
        """Test float path parameter coercion."""
        async with WebSocketTestClient(api, "/ws/price/19.99") as ws:
            response = await ws.receive_json()
            assert response["price"] == 19.99
            assert response["type"] == "float"

    @pytest.mark.asyncio
    async def test_bool_coercion_true(self, api):
        """Test bool path parameter coercion (true)."""
        async with WebSocketTestClient(api, "/ws/active/true") as ws:
            response = await ws.receive_json()
            assert response["active"] is True
            assert response["type"] == "bool"

    @pytest.mark.asyncio
    async def test_bool_coercion_false(self, api):
        """Test bool path parameter coercion (false)."""
        async with WebSocketTestClient(api, "/ws/active/false") as ws:
            response = await ws.receive_json()
            assert response["active"] is False
            assert response["type"] == "bool"

    @pytest.mark.asyncio
    async def test_bool_coercion_one(self, api):
        """Test bool path parameter coercion (1)."""
        async with WebSocketTestClient(api, "/ws/active/1") as ws:
            response = await ws.receive_json()
            assert response["active"] is True
            assert response["type"] == "bool"


class TestWebSocketAnnotatedTypes:
    """Test WebSocket path parameter type coercion with typing.Annotated."""

    @pytest.fixture
    def api(self):
        api = BoltAPI()

        @api.websocket("/ws/annotated/{user_id}")
        async def annotated_handler(websocket: WebSocket, user_id: Annotated[int, "positive integer"]):
            await websocket.accept()
            await websocket.send_json(
                {
                    "user_id": user_id,
                    "type": type(user_id).__name__,
                }
            )

        @api.websocket("/ws/annotated_float/{price}")
        async def annotated_float_handler(websocket: WebSocket, price: Annotated[float, "price in USD"]):
            await websocket.accept()
            await websocket.send_json(
                {
                    "price": price,
                    "type": type(price).__name__,
                }
            )

        return api

    @pytest.mark.asyncio
    async def test_annotated_int_coercion(self, api):
        """Test integer coercion with Annotated type."""
        async with WebSocketTestClient(api, "/ws/annotated/123") as ws:
            response = await ws.receive_json()
            assert response["user_id"] == 123
            assert response["type"] == "int"

    @pytest.mark.asyncio
    async def test_annotated_float_coercion(self, api):
        """Test float coercion with Annotated type."""
        async with WebSocketTestClient(api, "/ws/annotated_float/29.99") as ws:
            response = await ws.receive_json()
            assert response["price"] == 29.99
            assert response["type"] == "float"

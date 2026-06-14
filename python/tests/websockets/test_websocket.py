"""
WebSocket tests using WebSocketTestClient.

Tests the WebSocket functionality including:
- Echo (text, json, binary)
- Path parameters with type coercion
- Query parameters (via Query() marker)
- Headers (via Header() marker)
- Cookies (via Cookie() marker)
- Close handling
- Guards/auth integration with real JWT tokens
- Error handling
"""

from __future__ import annotations

import contextlib
import time
from typing import Annotated

import jwt
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import HasPermission, IsAdminUser, IsAuthenticated, JWTAuthentication
from django_bolt.param_functions import Cookie, Header, Query
from django_bolt.testing import ConnectionClosed, WebSocketTestClient
from django_bolt.websocket import CloseCode

# Test secret for JWT tokens
TEST_JWT_SECRET = "test-secret-key-for-websocket-tests"


def create_test_jwt(
    user_id: int = 123,
    is_staff: bool = False,
    is_superuser: bool = False,
    permissions: list[str] | None = None,
) -> str:
    """Create a JWT token for testing."""
    payload = {
        "sub": str(user_id),
        "user_id": str(user_id),
        "is_staff": is_staff,
        "is_superuser": is_superuser,
        "permissions": permissions or [],
        "exp": int(time.time()) + 3600,  # 1 hour from now
        "iat": int(time.time()),
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def api():
    """Create test API with WebSocket routes."""
    api = BoltAPI()

    # Echo handler
    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        with contextlib.suppress(Exception):
            async for message in websocket.iter_text():
                await websocket.send_text(f"Echo: {message}")

    # JSON handler
    @api.websocket("/ws/json")
    async def json_handler(websocket: WebSocket):
        await websocket.accept()
        with contextlib.suppress(Exception):
            async for data in websocket.iter_json():
                response = {
                    "received": data,
                    "type": type(data).__name__,
                }
                await websocket.send_json(response)

    # Binary handler
    @api.websocket("/ws/binary")
    async def binary_handler(websocket: WebSocket):
        await websocket.accept()
        with contextlib.suppress(Exception):
            async for data in websocket.iter_bytes():
                # Echo back with length prefix
                await websocket.send_bytes(len(data).to_bytes(4, "big") + data)

    # Path params handler
    @api.websocket("/ws/chat/{room_id}")
    async def chat_handler(websocket: WebSocket, room_id: str):
        await websocket.accept()
        await websocket.send_text(f"Joined room: {room_id}")
        with contextlib.suppress(Exception):
            async for message in websocket.iter_text():
                await websocket.send_text(f"[{room_id}] {message}")

    # Multiple path params
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

    # Query params handler
    @api.websocket("/ws/auth")
    async def auth_query_handler(websocket: WebSocket):
        await websocket.accept()
        token = websocket.query_params.get("token", "none")
        await websocket.send_json({"authenticated": token != "none", "token": token})

    # Headers handler
    @api.websocket("/ws/headers")
    async def headers_handler(websocket: WebSocket):
        await websocket.accept()
        user_agent = websocket.headers.get("user-agent", "unknown")
        custom = websocket.headers.get("x-custom-header", "none")
        await websocket.send_json(
            {
                "user_agent": user_agent,
                "custom_header": custom,
            }
        )

    # Close handlers
    @api.websocket("/ws/close-normal")
    async def close_normal_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close(code=CloseCode.NORMAL, reason="done")

    @api.websocket("/ws/close-error")
    async def close_error_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.close(code=CloseCode.INTERNAL_ERROR, reason="something went wrong")

    # Type coercion handlers
    @api.websocket("/ws/typed/user/{user_id}")
    async def user_handler(websocket: WebSocket, user_id: int):
        await websocket.accept()
        await websocket.send_json(
            {
                "user_id": user_id,
                "type": type(user_id).__name__,
            }
        )

    @api.websocket("/ws/typed/price/{price}")
    async def price_handler(websocket: WebSocket, price: float):
        await websocket.accept()
        await websocket.send_json(
            {
                "price": price,
                "type": type(price).__name__,
            }
        )

    @api.websocket("/ws/typed/active/{active}")
    async def active_handler(websocket: WebSocket, active: bool):
        await websocket.accept()
        await websocket.send_json(
            {
                "active": active,
                "type": type(active).__name__,
            }
        )

    # Guard-protected handlers with real JWT authentication
    @api.websocket("/ws/public")
    async def public_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("public")

    @api.websocket(
        "/ws/protected",
        auth=[JWTAuthentication(secret=TEST_JWT_SECRET)],
        guards=[IsAuthenticated()],
    )
    async def protected_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("protected")

    @api.websocket(
        "/ws/admin",
        auth=[JWTAuthentication(secret=TEST_JWT_SECRET)],
        guards=[IsAdminUser()],
    )
    async def admin_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("admin")

    @api.websocket(
        "/ws/permission",
        auth=[JWTAuthentication(secret=TEST_JWT_SECRET)],
        guards=[HasPermission("api.view_data")],
    )
    async def permission_ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("has permission")

    # Error handler
    @api.websocket("/ws/error")
    async def error_handler(websocket: WebSocket):
        await websocket.accept()
        raise ValueError("Test error")

    # Slow handler for timeout test
    @api.websocket("/ws/slow")
    async def slow_handler(websocket: WebSocket):
        await websocket.accept()
        # Don't send anything - client should timeout

    return api


# --- Echo Tests ---


@pytest.mark.asyncio
async def test_websocket_echo_text(api):
    """Test basic text echo."""
    async with WebSocketTestClient(api, "/ws/echo") as ws:
        await ws.send_text("hello")
        response = await ws.receive_text()
        assert response == "Echo: hello"


@pytest.mark.asyncio
async def test_websocket_echo_multiple(api):
    """Test multiple messages in sequence."""
    async with WebSocketTestClient(api, "/ws/echo") as ws:
        for i in range(3):
            await ws.send_text(f"message {i}")
            response = await ws.receive_text()
            assert response == f"Echo: message {i}"


@pytest.mark.asyncio
async def test_websocket_echo_unicode(api):
    """Test echoing unicode characters."""
    async with WebSocketTestClient(api, "/ws/echo") as ws:
        await ws.send_text("Hello 世界")
        response = await ws.receive_text()
        assert response == "Echo: Hello 世界"


# --- JSON Tests ---


@pytest.mark.asyncio
async def test_websocket_json_dict(api):
    """Test sending and receiving JSON dict."""
    async with WebSocketTestClient(api, "/ws/json") as ws:
        await ws.send_json({"name": "test", "value": 42})
        response = await ws.receive_json()
        assert response["received"] == {"name": "test", "value": 42}
        assert response["type"] == "dict"


@pytest.mark.asyncio
async def test_websocket_json_list(api):
    """Test sending and receiving JSON list."""
    async with WebSocketTestClient(api, "/ws/json") as ws:
        await ws.send_json([1, 2, 3, "four"])
        response = await ws.receive_json()
        assert response["received"] == [1, 2, 3, "four"]
        assert response["type"] == "list"


# --- Binary Tests ---


@pytest.mark.asyncio
async def test_websocket_binary(api):
    """Test binary message echo."""
    async with WebSocketTestClient(api, "/ws/binary") as ws:
        data = b"\x00\x01\x02\x03\x04"
        await ws.send_bytes(data)
        response = await ws.receive_bytes()
        length = int.from_bytes(response[:4], "big")
        assert length == len(data)
        assert response[4:] == data


# --- Path Parameter Tests ---


@pytest.mark.asyncio
async def test_websocket_path_param(api):
    """Test WebSocket with path parameter."""
    async with WebSocketTestClient(api, "/ws/chat/general") as ws:
        welcome = await ws.receive_text()
        assert welcome == "Joined room: general"

        await ws.send_text("hello everyone")
        response = await ws.receive_text()
        assert response == "[general] hello everyone"


@pytest.mark.asyncio
async def test_websocket_multiple_path_params(api):
    """Test WebSocket with multiple path parameters."""
    async with WebSocketTestClient(api, "/ws/user/42/channel/tech") as ws:
        response = await ws.receive_json()
        assert response == {
            "user_id": "42",
            "channel_id": "tech",
            "status": "connected",
        }


# --- Query Parameter Tests ---


@pytest.mark.asyncio
async def test_websocket_query_param(api):
    """Test accessing query parameters."""
    async with WebSocketTestClient(api, "/ws/auth", query_string="token=secret123") as ws:
        response = await ws.receive_json()
        assert response["authenticated"] is True
        assert response["token"] == "secret123"


@pytest.mark.asyncio
async def test_websocket_no_query_param(api):
    """Test without query parameters."""
    async with WebSocketTestClient(api, "/ws/auth") as ws:
        response = await ws.receive_json()
        assert response["authenticated"] is False
        assert response["token"] == "none"


# --- Header Tests ---


@pytest.mark.asyncio
async def test_websocket_headers(api):
    """Test accessing custom headers."""
    headers = {
        "User-Agent": "TestClient/1.0",
        "X-Custom-Header": "custom-value",
    }
    async with WebSocketTestClient(api, "/ws/headers", headers=headers) as ws:
        response = await ws.receive_json()
        assert response["user_agent"] == "TestClient/1.0"
        assert response["custom_header"] == "custom-value"


# --- Close Tests ---


@pytest.mark.asyncio
async def test_websocket_server_close_normal(api):
    """Test server-initiated normal close."""
    async with WebSocketTestClient(api, "/ws/close-normal") as ws:
        msg = await ws.receive_text()
        assert msg == "hello"

        with pytest.raises(ConnectionClosed) as exc_info:
            await ws.receive_text()

        assert exc_info.value.code == CloseCode.NORMAL


@pytest.mark.asyncio
async def test_websocket_server_close_error(api):
    """Test server-initiated error close."""
    async with WebSocketTestClient(api, "/ws/close-error") as ws:
        with pytest.raises(ConnectionClosed) as exc_info:
            await ws.receive_text()

        assert exc_info.value.code == CloseCode.INTERNAL_ERROR


# --- Type Coercion Tests ---


@pytest.mark.asyncio
async def test_websocket_int_coercion(api):
    """Test integer path parameter coercion."""
    async with WebSocketTestClient(api, "/ws/typed/user/42") as ws:
        response = await ws.receive_json()
        assert response["user_id"] == 42
        assert response["type"] == "int"


@pytest.mark.asyncio
async def test_websocket_float_coercion(api):
    """Test float path parameter coercion."""
    async with WebSocketTestClient(api, "/ws/typed/price/19.99") as ws:
        response = await ws.receive_json()
        assert response["price"] == 19.99
        assert response["type"] == "float"


@pytest.mark.asyncio
async def test_websocket_bool_coercion(api):
    """Test bool path parameter coercion."""
    async with WebSocketTestClient(api, "/ws/typed/active/true") as ws:
        response = await ws.receive_json()
        assert response["active"] is True
        assert response["type"] == "bool"

    async with WebSocketTestClient(api, "/ws/typed/active/false") as ws:
        response = await ws.receive_json()
        assert response["active"] is False
        assert response["type"] == "bool"


# --- Guard Tests ---


@pytest.mark.asyncio
async def test_websocket_public_route(api):
    """Test public route works without auth."""
    async with WebSocketTestClient(api, "/ws/public") as ws:
        msg = await ws.receive_text()
        assert msg == "public"


@pytest.mark.asyncio
async def test_websocket_protected_without_auth(api):
    """Test protected route fails without auth context."""
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(api, "/ws/protected"):
            pass

    assert "Authentication required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_websocket_protected_with_auth(api):
    """Test protected route works with valid JWT token."""
    # Create a valid JWT token
    token = create_test_jwt(user_id=123)
    headers = {"Authorization": f"Bearer {token}"}

    async with WebSocketTestClient(api, "/ws/protected", headers=headers) as ws:
        msg = await ws.receive_text()
        assert msg == "protected"


@pytest.mark.asyncio
async def test_websocket_admin_route(api):
    """Test admin route requires superuser."""
    # Regular user token (not superuser)
    regular_token = create_test_jwt(user_id=123, is_superuser=False)
    regular_headers = {"Authorization": f"Bearer {regular_token}"}

    # Regular user should fail
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(api, "/ws/admin", headers=regular_headers):
            pass
    assert "Permission denied" in str(exc_info.value)

    # Admin token (superuser)
    admin_token = create_test_jwt(user_id=123, is_superuser=True, is_staff=True)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Admin should succeed
    async with WebSocketTestClient(api, "/ws/admin", headers=admin_headers) as ws:
        msg = await ws.receive_text()
        assert msg == "admin"


@pytest.mark.asyncio
async def test_websocket_permission_route(api):
    """Test permission-guarded route."""
    # User without permission
    no_perm_token = create_test_jwt(user_id=123, permissions=[])
    no_perm_headers = {"Authorization": f"Bearer {no_perm_token}"}

    # Without permission should fail
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(api, "/ws/permission", headers=no_perm_headers):
            pass
    assert "Permission denied" in str(exc_info.value)

    # User with permission
    with_perm_token = create_test_jwt(user_id=123, permissions=["api.view_data"])
    with_perm_headers = {"Authorization": f"Bearer {with_perm_token}"}

    # With permission should succeed
    async with WebSocketTestClient(api, "/ws/permission", headers=with_perm_headers) as ws:
        msg = await ws.receive_text()
        assert msg == "has permission"


# --- Error Handling Tests ---


@pytest.mark.asyncio
async def test_websocket_handler_error(api):
    """Test handler error handling."""
    with pytest.raises(ValueError) as exc_info:
        async with WebSocketTestClient(api, "/ws/error"):
            pass

    assert "Test error" in str(exc_info.value)


@pytest.mark.asyncio
async def test_websocket_receive_timeout(api):
    """Test receive timeout."""
    async with WebSocketTestClient(api, "/ws/slow") as ws:
        with pytest.raises(TimeoutError):
            await ws.receive_text(timeout=0.1)


# --- State Tests ---


@pytest.mark.asyncio
async def test_websocket_connection_state(api):
    """Test connection state tracking."""
    ws = WebSocketTestClient(api, "/ws/echo")
    assert ws.closed is False

    async with ws:
        await ws.send_text("test")
        await ws.receive_text()
        assert ws.accepted is True
        assert ws.closed is False

    assert ws.closed is True


# --- Parameter Injection Tests ---
# These tests verify that the production injector code path works correctly.
# They use Query(), Header(), and Cookie() markers which require the
# pre-compiled injector (not manual parameter extraction).


@pytest.fixture
def injection_api():
    """Create test API with parameter injection WebSocket routes."""
    api = BoltAPI()

    # Query parameter injection via Query() marker
    @api.websocket("/ws/inject/query")
    async def query_inject_handler(
        websocket: WebSocket,
        token: Annotated[str, Query()],
        limit: Annotated[int, Query()] = 10,
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "token": token,
                "token_type": type(token).__name__,
                "limit": limit,
                "limit_type": type(limit).__name__,
            }
        )

    # Header injection via Header() marker
    @api.websocket("/ws/inject/header")
    async def header_inject_handler(
        websocket: WebSocket,
        authorization: Annotated[str, Header()],
        x_request_id: Annotated[str, Header(alias="x-request-id")] = "default",
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "authorization": authorization,
                "x_request_id": x_request_id,
            }
        )

    # Cookie injection via Cookie() marker
    @api.websocket("/ws/inject/cookie")
    async def cookie_inject_handler(
        websocket: WebSocket,
        session_id: Annotated[str, Cookie(alias="session")],
        theme: Annotated[str, Cookie()] = "light",
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "session_id": session_id,
                "theme": theme,
            }
        )

    # Mixed injection (path + query + header + cookie)
    @api.websocket("/ws/inject/mixed/{room_id}")
    async def mixed_inject_handler(
        websocket: WebSocket,
        room_id: int,  # Path param with type coercion
        token: Annotated[str, Query()],
        authorization: Annotated[str, Header()],
        session: Annotated[str, Cookie(alias="session")],
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "room_id": room_id,
                "room_id_type": type(room_id).__name__,
                "token": token,
                "authorization": authorization,
                "session": session,
            }
        )

    return api


@pytest.mark.asyncio
async def test_websocket_query_injection(injection_api):
    """Test Query() parameter injection works via production injector."""
    async with WebSocketTestClient(injection_api, "/ws/inject/query", query_string="token=abc123&limit=50") as ws:
        response = await ws.receive_json()
        assert response["token"] == "abc123"
        assert response["token_type"] == "str"
        assert response["limit"] == 50
        assert response["limit_type"] == "int"


@pytest.mark.asyncio
async def test_websocket_query_injection_default(injection_api):
    """Test Query() parameter uses default value when not provided."""
    async with WebSocketTestClient(injection_api, "/ws/inject/query", query_string="token=xyz") as ws:
        response = await ws.receive_json()
        assert response["token"] == "xyz"
        assert response["limit"] == 10  # Default value


@pytest.mark.asyncio
async def test_websocket_header_injection(injection_api):
    """Test Header() parameter injection works via production injector."""
    headers = {
        "Authorization": "Bearer secret-token",
        "X-Request-ID": "req-12345",
    }
    async with WebSocketTestClient(injection_api, "/ws/inject/header", headers=headers) as ws:
        response = await ws.receive_json()
        assert response["authorization"] == "Bearer secret-token"
        assert response["x_request_id"] == "req-12345"


@pytest.mark.asyncio
async def test_websocket_header_injection_default(injection_api):
    """Test Header() parameter uses default value when not provided."""
    headers = {
        "Authorization": "Bearer token",
    }
    async with WebSocketTestClient(injection_api, "/ws/inject/header", headers=headers) as ws:
        response = await ws.receive_json()
        assert response["authorization"] == "Bearer token"
        assert response["x_request_id"] == "default"


@pytest.mark.asyncio
async def test_websocket_cookie_injection(injection_api):
    """Test Cookie() parameter injection works via production injector."""
    headers = {
        "Cookie": "session=sess-abc123; theme=dark",
    }
    async with WebSocketTestClient(injection_api, "/ws/inject/cookie", headers=headers) as ws:
        response = await ws.receive_json()
        assert response["session_id"] == "sess-abc123"
        assert response["theme"] == "dark"


@pytest.mark.asyncio
async def test_websocket_cookie_injection_default(injection_api):
    """Test Cookie() parameter uses default value when not provided."""
    headers = {
        "Cookie": "session=my-session",
    }
    async with WebSocketTestClient(injection_api, "/ws/inject/cookie", headers=headers) as ws:
        response = await ws.receive_json()
        assert response["session_id"] == "my-session"
        assert response["theme"] == "light"  # Default value


@pytest.mark.asyncio
async def test_websocket_mixed_injection(injection_api):
    """Test combined path, query, header, and cookie parameter injection."""
    headers = {
        "Authorization": "Bearer mix-token",
        "Cookie": "session=mix-session",
    }
    async with WebSocketTestClient(
        injection_api, "/ws/inject/mixed/42", query_string="token=mix-query-token", headers=headers
    ) as ws:
        response = await ws.receive_json()
        # Path param with type coercion
        assert response["room_id"] == 42
        assert response["room_id_type"] == "int"
        # Query param
        assert response["token"] == "mix-query-token"
        # Header param
        assert response["authorization"] == "Bearer mix-token"
        # Cookie param
        assert response["session"] == "mix-session"

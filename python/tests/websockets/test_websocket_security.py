"""WebSocket security tests.

Tests security features that use the same infrastructure as HTTP:
- Origin validation (uses CORS config)
- Rate limiting (reuses HTTP rate limit decorator)
- Authentication/Guards (already tested in test_websocket.py)
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.middleware import rate_limit
from django_bolt.testing import WebSocketTestClient

# --- Origin Validation Tests ---


@pytest.mark.asyncio
async def test_websocket_origin_allowed():
    """Test WebSocket connection with allowed origin succeeds."""
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # Configure CORS with specific allowed origin
    async with WebSocketTestClient(
        api,
        "/ws/echo",
        headers={"Origin": "https://example.com"},
        cors_allowed_origins=["https://example.com"],
        read_django_settings=False,  # Don't read from Django settings
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "connected"


@pytest.mark.asyncio
async def test_websocket_origin_denied():
    """Test WebSocket connection with disallowed origin is rejected."""
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # Configure CORS with specific allowed origin, but request from different origin
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(
            api,
            "/ws/echo",
            headers={"Origin": "https://evil.com"},
            cors_allowed_origins=["https://example.com"],
            read_django_settings=False,
        ):
            pass

    assert "Origin not allowed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_websocket_no_origin_same_origin_allowed():
    """Test WebSocket without Origin header (same-origin request) is allowed."""
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # No Origin header = same-origin request, should be allowed
    async with WebSocketTestClient(
        api,
        "/ws/echo",
        headers={},  # No Origin header
        cors_allowed_origins=["https://example.com"],
        read_django_settings=False,
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "connected"


@pytest.mark.asyncio
async def test_websocket_origin_wildcard_allows_all():
    """Test WebSocket with wildcard CORS origin allows all origins."""
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # Wildcard origin should allow any origin
    async with WebSocketTestClient(
        api,
        "/ws/echo",
        headers={"Origin": "https://any-site.com"},
        cors_allowed_origins=["*"],
        read_django_settings=False,
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "connected"


@pytest.mark.asyncio
async def test_websocket_no_cors_config_denies_cross_origin():
    """Test WebSocket without CORS config denies cross-origin requests (fail-secure)."""
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # No CORS config + Origin header = should be denied (fail-secure)
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(
            api,
            "/ws/echo",
            headers={"Origin": "https://example.com"},
            cors_allowed_origins=None,  # No CORS config
            read_django_settings=False,  # Don't fall back to Django settings
        ):
            pass

    assert "Origin not allowed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_websocket_no_cors_config_allows_same_origin():
    """Test WebSocket without CORS config allows same-origin requests (no Origin header)."""
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # No CORS config + no Origin header = same-origin, should be allowed
    async with WebSocketTestClient(
        api,
        "/ws/echo",
        headers={},  # No Origin header
        cors_allowed_origins=None,  # No CORS config
        read_django_settings=False,
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "connected"


# --- Parameter Length Limit Tests ---


@pytest.mark.asyncio
async def test_websocket_oversized_query_param_rejected():
    """Oversized query param rejects the upgrade instead of passing a raw string through.

    Before the fix, a length-limit violation was swallowed by the string fallback,
    so the oversized value reached the handler. Now it is a hard error.
    """
    api = BoltAPI()

    @api.websocket("/ws/echo")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    oversized = "a" * 9000  # exceeds the default 8192-byte limit

    with pytest.raises(ValueError, match=r"Parameter too long"):
        async with WebSocketTestClient(
            api,
            "/ws/echo",
            query_string=f"value={oversized}",
            cors_allowed_origins=["*"],
            read_django_settings=False,
        ):
            pass


@pytest.mark.asyncio
async def test_websocket_oversized_path_param_rejected():
    """Oversized path param rejects the upgrade instead of passing a raw string through."""
    api = BoltAPI()

    @api.websocket("/ws/{value}")
    async def echo_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    oversized = "a" * 9000  # exceeds the default 8192-byte limit

    with pytest.raises(ValueError, match=r"Parameter too long"):
        async with WebSocketTestClient(
            api,
            f"/ws/{oversized}",
            cors_allowed_origins=["*"],
            read_django_settings=False,
        ):
            pass


# --- Rate Limiting Tests ---


@pytest.mark.asyncio
async def test_websocket_rate_limit_exceeded():
    """Test WebSocket connection rejected when rate limit is exceeded."""
    api = BoltAPI()

    @api.websocket("/ws/rate-limited")
    @rate_limit(rps=1, burst=2)  # Allow burst of 2, then rate limit kicks in
    async def rate_limited_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # First connection should succeed (burst allows 2)
    async with WebSocketTestClient(
        api,
        "/ws/rate-limited",
        cors_allowed_origins=["*"],
        read_django_settings=False,
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "connected"

    # Second connection should succeed (burst allows 2)
    async with WebSocketTestClient(
        api,
        "/ws/rate-limited",
        cors_allowed_origins=["*"],
        read_django_settings=False,
    ) as ws:
        msg = await ws.receive_text()
        assert msg == "connected"

    # Third connection should fail (burst of 2 exceeded, no time for refill)
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(
            api,
            "/ws/rate-limited",
            cors_allowed_origins=["*"],
            read_django_settings=False,
        ):
            pass

    assert "Rate limit exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_websocket_no_rate_limit():
    """Test WebSocket without rate limit allows unlimited connections."""
    api = BoltAPI()

    @api.websocket("/ws/unlimited")
    async def unlimited_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # Multiple connections should all succeed
    for _i in range(5):
        async with WebSocketTestClient(
            api,
            "/ws/unlimited",
            cors_allowed_origins=["*"],
            read_django_settings=False,
        ) as ws:
            msg = await ws.receive_text()
            assert msg == "connected"


@pytest.mark.asyncio
async def test_websocket_origin_check_before_rate_limit():
    """Test origin is validated before rate limiting is applied.

    Origin validation runs BEFORE rate limiting in Rust (see test_state.rs:261-285),
    so origin-rejected requests return immediately without reaching the rate limiter.
    """
    api = BoltAPI()

    @api.websocket("/ws/origin-ratelimit-order")
    @rate_limit(rps=1, burst=1)
    async def handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    # Origin-denied request should fail with origin error, not rate limit error
    with pytest.raises(PermissionError) as exc_info:
        async with WebSocketTestClient(
            api,
            "/ws/origin-ratelimit-order",
            headers={"Origin": "https://evil.com"},
            cors_allowed_origins=["https://example.com"],
            read_django_settings=False,
        ):
            pass

    # Verify the error is about origin, not rate limiting
    assert "Origin not allowed" in str(exc_info.value)
    assert "Rate limit" not in str(exc_info.value)

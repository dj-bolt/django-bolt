"""Testing utilities for django-bolt.

Provides test clients for in-memory testing without subprocess/network overhead,
including streaming response support for SSE and similar endpoints.

Usage:
    # Regular HTTP response
    response = client.get("/endpoint")
    assert response.status_code == 200

    # Streaming response with chunk iteration
    response = client.get("/sse", stream=True)
    for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
        process(chunk)

    # Streaming response with line iteration
    response = client.get("/sse", stream=True)
    for line in response.iter_lines():
        process(line)

    # WebSocket testing
    async with WebSocketTestClient(api, "/ws/echo") as ws:
        await ws.send_text("hello")
        response = await ws.receive_text()
        assert response == "Echo: hello"
"""

from django_bolt.testing.client import AsyncTestClient, BoltTestClientWarning, TestClient
from django_bolt.testing.websocket import ConnectionClosed, WebSocketTestClient

__all__ = [
    "TestClient",
    "AsyncTestClient",
    "BoltTestClientWarning",
    "WebSocketTestClient",
    "ConnectionClosed",
]

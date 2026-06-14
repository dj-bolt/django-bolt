"""Tests for EventSourceResponse, ServerSentEvent, and format_sse_event."""

from __future__ import annotations

from collections.abc import AsyncIterable

import msgspec
import pytest

from django_bolt import BoltAPI, EventSourceResponse, ServerSentEvent, format_sse_event
from django_bolt.testing import TestClient

# ═══════════════════════════════════════════════════════════════════════════
# Unit tests: format_sse_event
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatSSEEvent:
    def test_data_only(self):
        result = format_sse_event(data_str='{"msg":"hello"}')
        assert result == b'data: {"msg":"hello"}\n\n'

    def test_multiline_data(self):
        result = format_sse_event(data_str="line1\nline2\nline3")
        assert result == b"data: line1\ndata: line2\ndata: line3\n\n"

    def test_all_fields(self):
        result = format_sse_event(
            data_str="payload",
            event="update",
            id="42",
            retry=5000,
            comment="keepalive",
        )
        assert result == b": keepalive\nevent: update\ndata: payload\nid: 42\nretry: 5000\n\n"

    def test_comment_only(self):
        result = format_sse_event(comment="ping")
        assert result == b": ping\n\n"

    def test_event_and_data(self):
        result = format_sse_event(data_str="hello", event="greeting")
        assert result == b"event: greeting\ndata: hello\n\n"

    def test_retry_only(self):
        result = format_sse_event(retry=3000)
        assert result == b"retry: 3000\n\n"

    def test_empty_event(self):
        result = format_sse_event()
        assert result == b"\n"

    def test_multiline_comment(self):
        result = format_sse_event(comment="line1\nline2")
        assert result == b": line1\n: line2\n\n"


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests: ServerSentEvent
# ═══════════════════════════════════════════════════════════════════════════


class TestServerSentEvent:
    def test_data_only(self):
        event = ServerSentEvent(data={"key": "value"})
        assert event.data == {"key": "value"}
        assert event.raw_data is None

    def test_raw_data_only(self):
        event = ServerSentEvent(raw_data="plain text")
        assert event.raw_data == "plain text"
        assert event.data is None

    def test_data_and_raw_data_exclusive(self):
        with pytest.raises(ValueError, match="Cannot set both"):
            ServerSentEvent(data="hello", raw_data="world")

    def test_id_no_null(self):
        with pytest.raises(ValueError, match="must not contain null"):
            ServerSentEvent(data="x", id="abc\0def")

    def test_retry_negative(self):
        with pytest.raises(ValueError, match="non-negative"):
            ServerSentEvent(retry=-1)

    def test_all_fields(self):
        event = ServerSentEvent(
            data={"count": 1},
            event="update",
            id="1",
            retry=5000,
            comment="info",
        )
        assert event.event == "update"
        assert event.id == "1"
        assert event.retry == 5000
        assert event.comment == "info"

    def test_frozen(self):
        event = ServerSentEvent(data="hello")
        with pytest.raises(AttributeError):
            event.data = "world"


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests: EventSourceResponse (explicit pattern)
# ═══════════════════════════════════════════════════════════════════════════


class Item(msgspec.Struct):
    name: str
    price: float


@pytest.fixture(scope="module")
def api():
    api = BoltAPI()

    @api.get("/sse-explicit-dict")
    async def sse_explicit_dict():
        async def gen():
            yield {"message": "hello"}
            yield {"message": "world"}

        return EventSourceResponse(gen())

    @api.get("/sse-explicit-sse-event")
    async def sse_explicit_sse_event():
        async def gen():
            yield ServerSentEvent(data={"count": 1}, event="update", id="1")
            yield ServerSentEvent(data={"count": 2}, event="update", id="2")

        return EventSourceResponse(gen())

    @api.get("/sse-explicit-mixed")
    async def sse_explicit_mixed():
        async def gen():
            yield {"auto": True}
            yield ServerSentEvent(data="manual", event="custom")
            yield ServerSentEvent(raw_data="raw text line")

        return EventSourceResponse(gen())

    @api.get("/sse-explicit-string")
    async def sse_explicit_string():
        """Strings are passed through raw (user controls framing)."""

        async def gen():
            yield "data: raw-sse\n\n"

        return EventSourceResponse(gen())

    @api.get("/sse-explicit-struct")
    async def sse_explicit_struct():
        async def gen():
            yield Item(name="Widget", price=9.99)

        return EventSourceResponse(gen())

    # ── Implicit pattern: response_class=EventSourceResponse ──

    @api.get("/sse-implicit", response_class=EventSourceResponse)
    async def sse_implicit() -> AsyncIterable[dict]:
        yield {"message": "implicit-hello"}
        yield {"message": "implicit-world"}

    @api.get("/sse-implicit-struct", response_class=EventSourceResponse)
    async def sse_implicit_struct() -> AsyncIterable[Item]:
        yield Item(name="Gadget", price=19.99)
        yield Item(name="Doohickey", price=4.50)

    @api.get("/sse-implicit-sse-event", response_class=EventSourceResponse)
    async def sse_implicit_sse_event() -> AsyncIterable[ServerSentEvent]:
        yield ServerSentEvent(data={"n": 1}, event="tick", id="1")
        yield ServerSentEvent(data={"n": 2}, event="tick", id="2")

    # ── Sync generators ──

    @api.get("/sse-sync-explicit")
    def sse_sync_explicit():
        def gen():
            yield {"sync": True}
            yield {"sync": False}

        return EventSourceResponse(gen())

    @api.get("/sse-sync-implicit", response_class=EventSourceResponse)
    def sse_sync_implicit():
        yield {"sync": "implicit"}

    return api


@pytest.fixture(scope="module")
def client(api):
    with TestClient(api) as c:
        yield c


# ── Explicit pattern tests ──


class TestExplicitEventSourceResponse:
    def test_dict_yield(self, client):
        response = client.get("/sse-explicit-dict")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.content.decode()
        assert 'data: {"message":"hello"}' in body
        assert 'data: {"message":"world"}' in body

    def test_server_sent_event_yield(self, client):
        response = client.get("/sse-explicit-sse-event")
        assert response.status_code == 200
        body = response.content.decode()
        assert "event: update" in body
        assert "id: 1" in body
        assert 'data: {"count":1}' in body
        assert "id: 2" in body
        assert 'data: {"count":2}' in body

    def test_mixed_yield(self, client):
        response = client.get("/sse-explicit-mixed")
        assert response.status_code == 200
        body = response.content.decode()
        # Dict auto-framed
        assert 'data: {"auto":true}' in body
        # ServerSentEvent with event type
        assert "event: custom" in body
        assert 'data: "manual"' in body
        # ServerSentEvent with raw_data
        assert "data: raw text line" in body

    def test_string_passthrough(self, client):
        response = client.get("/sse-explicit-string")
        assert response.status_code == 200
        body = response.content.decode()
        assert "data: raw-sse\n\n" in body

    def test_struct_yield(self, client):
        response = client.get("/sse-explicit-struct")
        assert response.status_code == 200
        body = response.content.decode()
        assert 'data: {"name":"Widget","price":9.99}' in body

    def test_sse_headers(self, client):
        response = client.get("/sse-explicit-dict")
        assert "text/event-stream" in response.headers["content-type"]


# ── Implicit pattern tests ──


class TestImplicitEventSourceResponse:
    def test_dict_yield(self, client):
        response = client.get("/sse-implicit")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.content.decode()
        assert 'data: {"message":"implicit-hello"}' in body
        assert 'data: {"message":"implicit-world"}' in body

    def test_struct_yield(self, client):
        response = client.get("/sse-implicit-struct")
        assert response.status_code == 200
        body = response.content.decode()
        assert 'data: {"name":"Gadget","price":19.99}' in body
        assert 'data: {"name":"Doohickey","price":4.5}' in body

    def test_sse_event_yield(self, client):
        response = client.get("/sse-implicit-sse-event")
        assert response.status_code == 200
        body = response.content.decode()
        assert "event: tick" in body
        assert "id: 1" in body
        assert 'data: {"n":1}' in body


# ── Sync generator tests ──


class TestSyncEventSourceResponse:
    def test_sync_explicit(self, client):
        response = client.get("/sse-sync-explicit")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.content.decode()
        assert 'data: {"sync":true}' in body
        assert 'data: {"sync":false}' in body

    def test_sync_implicit(self, client):
        response = client.get("/sse-sync-implicit")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.content.decode()
        assert 'data: {"sync":"implicit"}' in body

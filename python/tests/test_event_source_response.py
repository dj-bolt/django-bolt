"""Tests for EventSourceResponse, ServerSentEvent, and format_sse_event."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st

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

    def test_multiline_bytes_data(self):
        """Bytes data with a line break gets one data line for each line, as str data does."""
        assert format_sse_event(data_bytes=b"line1\nline2\r\nline3") == b"data: line1\ndata: line2\ndata: line3\n\n"

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

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"event": "e"}, b"event: e\ndata: a\ndata: b\n\n"),
            ({"id": "7"}, b"data: a\ndata: b\nid: 7\n\n"),
            ({"retry": 9}, b"data: a\ndata: b\nretry: 9\n\n"),
            ({"comment": "c"}, b": c\ndata: a\ndata: b\n\n"),
        ],
    )
    def test_data_bytes_is_framed_line_by_line_with_each_field(self, fields, expected):
        assert format_sse_event(data_bytes=b"a\nb", **fields) == expected


def _dispatched_events(stream: bytes) -> list[dict[str, str]]:
    """The events that a browser dispatches for an event stream.

    WHATWG HTML, section 9.2.6, "Interpreting an event stream": lines end at
    CRLF, LF or CR. A blank line dispatches the event when the data buffer is
    not empty, with one trailing LF removed. A space after the colon goes away.
    """
    events, data, event_type, last_event_id = [], "", "", ""
    for line in re.split(r"\r\n|\r|\n", stream.decode("utf-8")):
        if line == "":
            if data:
                events.append({"type": event_type or "message", "data": data.removesuffix("\n"), "id": last_event_id})
            data, event_type = "", ""
            continue
        if line.startswith(":"):
            continue
        name, colon, value = line.partition(":")
        value = value.removeprefix(" ") if colon else ""
        if name == "event":
            event_type = value
        elif name == "data":
            data += value + "\n"
        elif name == "id" and "\0" not in value:
            last_event_id = value
    return events


# A field value that a line can hold: no CR, no LF, no NUL. No text has a lone
# surrogate, because UTF-8 cannot encode it.
field_text = st.text(st.characters(exclude_categories=("Cs",), exclude_characters="\r\n\0"), min_size=1)
# Data text: each character, also CR, LF and the Unicode line separators.
data_text = st.text(st.characters(exclude_categories=("Cs",)))


class TestEventStreamRoundTrip:
    """Each event reads back in a browser with its data, its type and its id."""

    @given(
        data=data_text,
        as_bytes=st.booleans(),
        event=st.none() | field_text,
        event_id=st.none() | field_text,
        retry=st.none() | st.integers(min_value=0, max_value=10**6),
        comment=st.none() | data_text,
    )
    def test_an_event_reads_back_unchanged(self, data, as_bytes, event, event_id, retry, comment):
        payload = {"data_bytes": data.encode()} if as_bytes else {"data_str": data}
        wire = format_sse_event(**payload, event=event, id=event_id, retry=retry, comment=comment)
        # A browser joins the data lines with LF, so a CR or CRLF arrives as LF.
        received = data.replace("\r\n", "\n").replace("\r", "\n")
        assert _dispatched_events(wire) == [{"type": event or "message", "data": received, "id": event_id or ""}]

    @pytest.mark.parametrize("field", ["id", "event"])
    @pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n"])
    def test_a_line_break_in_id_or_event_is_refused(self, field, line_break):
        """A line break would end the field and add another field or event."""
        value = f"1{line_break}{line_break}data: extra"
        with pytest.raises(ValueError, match="line break"):
            format_sse_event(data_str="hi", **{field: value})
        with pytest.raises(ValueError, match="line break"):
            ServerSentEvent(data="hi", **{field: value})

    def test_a_unicode_line_separator_stays_in_the_data(self):
        """Only CR and LF end a line. JSON with U+2028 stays valid JSON."""
        data = msgspec.json.encode({"text": "a\u2028b\x85c"})
        [received] = _dispatched_events(format_sse_event(data_bytes=data, event="message"))
        assert msgspec.json.decode(received["data"]) == {"text": "a\u2028b\x85c"}

    @pytest.mark.parametrize("data", ["", "\n", "line\n", "\n\nend"])
    def test_empty_data_and_trailing_line_breaks_read_back(self, data):
        """An empty event is dispatched, and no line break is lost."""
        assert _dispatched_events(format_sse_event(data_str=data)) == [{"type": "message", "data": data, "id": ""}]


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


# ── Route status and event fields ──


def test_sse_route_keeps_its_status_and_frames_every_event_field():
    api = BoltAPI()

    # The handler has no await. Thus the sync data handler serializes its result.
    @api.get("/ev", status_code=201, response_class=EventSourceResponse)
    async def events():
        async def gen():
            yield bytearray(b": raw\n\n")
            yield memoryview(b": view\n\n")
            yield ServerSentEvent(raw_data="hello", event="update", id="7", retry=1000, comment="note")
            yield ServerSentEvent(data={"a": 1}, event="e", id="1", retry=500, comment="c")
            yield ServerSentEvent(comment="keepalive")

        return gen()

    with TestClient(api) as client:
        response = client.get("/ev")

    assert response.status_code == 201, response.text
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == (
        b": raw\n\n"
        b": view\n\n"
        b": note\nevent: update\ndata: hello\nid: 7\nretry: 1000\n\n"
        b': c\nevent: e\ndata: {"a":1}\nid: 1\nretry: 500\n\n'
        b": keepalive\n\n"
    )


def test_event_source_response_keeps_headers_and_ping_interval():
    api = BoltAPI()

    @api.get("/ev")
    def ev():
        async def gen():
            yield {"a": 1}

        response = EventSourceResponse(gen(), headers={"X-Custom": "yes"}, ping_interval=2.5)
        assert response.ping_interval == 2.5
        return response

    with TestClient(api) as client:
        response = client.get("/ev")

    assert response.headers["x-custom"] == "yes"
    assert response.content == b'data: {"a":1}\n\n'


def test_server_sent_event_retry_zero_is_valid():
    assert ServerSentEvent(retry=0).retry == 0

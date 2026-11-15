import pytest

from django_bolt.middleware_response import MiddlewareResponse
from django_bolt.responses import StreamingResponse
from django_bolt.serialization import _BODY_BYTES, _BODY_FILE, _BODY_STREAM


def test_middleware_response_does_not_promote_content_attribute_objects_to_stream():
    class ContentObject:
        def __init__(self):
            self.content = b"not-a-stream"

    response = MiddlewareResponse(
        status_code=200,
        headers={},
        body=ContentObject(),
        body_kind=_BODY_BYTES,
    )

    _status, _meta, body_kind, _payload = response.to_tuple()
    assert body_kind == _BODY_BYTES  # 0


def test_middleware_response_marks_streaming_response_body_as_stream():
    def gen():
        yield b"chunk"

    response = MiddlewareResponse(
        status_code=200,
        headers={},
        body=StreamingResponse(gen(), media_type="text/plain"),
        body_kind=_BODY_BYTES,
    )

    _status, _meta, body_kind, _payload = response.to_tuple()
    assert body_kind == _BODY_STREAM  # 1


def test_middleware_response_extracts_content_type_case_insensitively():
    response = MiddlewareResponse(
        status_code=200,
        headers={"Content-Type": "text/css", "X-Test": "1"},
        body="/tmp/example.css",
        response_type="file",
        body_kind=_BODY_FILE,
    )

    _status, meta, body_kind, payload = response.to_tuple()

    # The headers store each name in lower case. Rust sends each name in lower case too.
    assert meta == ("file", "text/css", [("x-test", "1")], None)
    assert body_kind == _BODY_FILE
    assert payload == "/tmp/example.css"


def test_middleware_response_adds_the_content_type_of_the_response_type_when_headers_have_none():
    from_init = MiddlewareResponse(200, {}, b"{}")
    assert dict(from_init.headers) == {"content-type": "application/json"}
    assert from_init.to_tuple()[1] == ("json", "application/json", None, None)

    # A tuple meta without a custom content type uses the one of the response type.
    from_meta = MiddlewareResponse.from_tuple((200, ("html", None, None, None), _BODY_BYTES, b"x"))
    assert dict(from_meta.headers) == {"content-type": "text/html; charset=utf-8"}

    # A custom content type wins over the one of the response type, in a tuple meta and in the headers.
    custom = MiddlewareResponse.from_tuple((200, ("html", "text/css", None, None), _BODY_BYTES, b"x"))
    assert dict(custom.headers) == {"content-type": "text/css"}
    assert MiddlewareResponse(200, {"Content-Type": "text/css"}, b"x", "html").headers["content-type"] == "text/css"

    # A response type with no content type gets none.
    empty = MiddlewareResponse.from_tuple((204, ("empty", None, None, None), _BODY_BYTES, b""))
    assert dict(empty.headers) == {}


def test_middleware_response_round_trips_cookies_and_plain_dict_headers():
    cookie = ("flavor", "oat", "/", None, None, None, False, False, None)
    response = MiddlewareResponse.from_tuple((200, ("json", None, None, [cookie]), _BODY_BYTES, b"{}"))
    assert response.to_tuple()[1] == ("json", "application/json", None, [cookie])

    # Middleware may replace the headers with a plain dict. Its content type keeps any letter case.
    response.headers = {"Content-Type": "text/css", "X-A": "1"}
    assert response.to_tuple()[1] == ("json", "text/css", [("X-A", "1")], [cookie])

    # The wire body is bytes, also when middleware sets a bytearray.
    response.body = bytearray(b"ab")
    payload = response.to_tuple()[3]
    assert type(payload) is bytes
    assert payload == b"ab"


@pytest.mark.parametrize("meta", [0, ("file", None, None, None)], ids=["tag", "tuple"])
@pytest.mark.parametrize("body_kind", [_BODY_STREAM, _BODY_FILE])
def test_middleware_response_keeps_the_body_kind_of_a_meta(meta, body_kind):
    # Meta tag 0 is JSON. The body kind must survive a round trip.
    response = MiddlewareResponse.from_tuple((200, meta, body_kind, "/tmp/x"))
    assert response.to_tuple()[2] == body_kind


@pytest.mark.parametrize(
    ("wire", "message"),
    [
        ([200, 0, _BODY_BYTES, b""], "ResponseWireV1 4-tuple"),
        ((200, 0, _BODY_BYTES), "ResponseWireV1 4-tuple"),
        ((200, 0, 9, b""), "Invalid middleware response body_kind 9"),
        ((200, 99, _BODY_BYTES, b""), "Unknown response meta tag 99"),
        ((200, ["json", None, None, None], _BODY_BYTES, b""), "Invalid middleware response metadata tuple"),
        ((200, ("json", None), _BODY_BYTES, b""), "Invalid middleware response metadata tuple"),
    ],
)
def test_middleware_response_rejects_a_malformed_wire_response(wire, message):
    with pytest.raises(TypeError, match=message):
        MiddlewareResponse.from_tuple(wire)

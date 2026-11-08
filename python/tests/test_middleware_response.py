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

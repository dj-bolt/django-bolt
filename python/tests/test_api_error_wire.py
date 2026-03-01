import pytest

from django_bolt.api import _wire_from_error_parts


@pytest.mark.parametrize(
    ("content_type", "expected_response_type"),
    [
        ("application/json", "json"),
        ("application/problem+json; charset=utf-8", "json"),
        ("text/plain; charset=utf-8", "plaintext"),
        ("text/html", "html"),
        ("application/xml", "octetstream"),
    ],
)
def test_wire_from_error_parts_infers_response_type_from_content_type(content_type, expected_response_type):
    status, meta, body_kind, body = _wire_from_error_parts(
        500,
        [("content-type", content_type), ("x-extra", "1")],
        b"payload",
    )

    response_type, custom_content_type, custom_headers, cookies = meta
    assert status == 500
    assert response_type == expected_response_type
    assert custom_content_type == content_type
    assert custom_headers == [("x-extra", "1")]
    assert cookies is None
    assert body_kind == 0  # _BODY_BYTES
    assert body == b"payload"

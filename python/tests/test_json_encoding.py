from __future__ import annotations

import msgspec
from django.utils.safestring import mark_safe

from django_bolt import _json


def test_encode_django_safe_string() -> None:
    value = mark_safe("<strong>Hello</strong>")

    encoded = _json.encode({"html": value})

    assert msgspec.json.decode(encoded) == {"html": "<strong>Hello</strong>"}
    assert type(msgspec.json.decode(encoded)["html"]) is str

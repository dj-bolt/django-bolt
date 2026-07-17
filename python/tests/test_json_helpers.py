"""Tests for the msgspec-backed JSON helpers in django_bolt._json."""

from __future__ import annotations

import enum
from datetime import date, datetime, time
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Address
from pathlib import Path, PurePath
from uuid import UUID

import msgspec
import pytest

from django_bolt import _json


class Color(enum.Enum):
    RED = "red"


class Item(msgspec.Struct):
    id: int


def test_default_serializer_enum():
    """Enum member serializes to its value."""
    assert _json.default_serializer(Color.RED) == "red"


def test_default_serializer_paths():
    """Path and PurePath serialize to str()."""
    p = Path("file.txt")
    pp = PurePath("dir/file.txt")
    assert _json.default_serializer(p) == str(p)
    assert _json.default_serializer(pp) == str(pp)


def test_default_serializer_datetimes():
    """datetime/date/time serialize via isoformat()."""
    dt = datetime(2020, 1, 2, 3, 4, 5)
    d = date(2020, 1, 2)
    t = time(3, 4, 5)
    assert _json.default_serializer(dt) == dt.isoformat()
    assert _json.default_serializer(d) == d.isoformat()
    assert _json.default_serializer(t) == t.isoformat()


def test_default_serializer_decimal_integral():
    """Decimal with non-negative exponent becomes an int."""
    result = _json.default_serializer(Decimal("5"))
    assert result == 5
    assert type(result) is int


def test_default_serializer_decimal_fractional():
    """Decimal with fractional part becomes a float."""
    result = _json.default_serializer(Decimal("5.5"))
    assert result == 5.5
    assert type(result) is float


def test_default_serializer_ip_types():
    """IP addresses, interfaces and networks serialize to str()."""
    assert _json.default_serializer(IPv4Address("1.2.3.4")) == "1.2.3.4"
    assert _json.default_serializer(IPv6Address("::1")) == "::1"
    assert _json.default_serializer(IPv4Interface("1.2.3.4/24")) == "1.2.3.4/24"
    assert _json.default_serializer(IPv4Network("10.0.0.0/8")) == "10.0.0.0/8"


def test_default_serializer_uuid():
    """UUID serializes to str()."""
    u = UUID("12345678-1234-5678-1234-567812345678")
    assert _json.default_serializer(u) == str(u)


def test_default_serializer_rejects_unsupported():
    """Unsupported types raise TypeError."""
    with pytest.raises(TypeError):
        _json.default_serializer(object())


def test_encode_roundtrips_plain_dict():
    """encode/decode round-trips a plain dict."""
    payload = {"a": 1, "b": ["x", "y"], "c": None}
    assert _json.decode(_json.encode(payload)) == payload


def test_encode_uses_hook_for_path():
    """A non-native Path in the payload is routed through the hook."""
    p = Path("hello.txt")
    encoded = _json.encode({"path": p})
    assert str(p).encode() in encoded


def test_encode_rejects_unsupported():
    """encode raises TypeError when a value has no encoder."""
    with pytest.raises(TypeError):
        _json.encode({"x": object()})


def test_encode_custom_serializer_override():
    """A custom serializer replaces the default hook."""

    class Widget:
        pass

    def serializer(value):
        if isinstance(value, Widget):
            return "widget"
        raise TypeError

    assert _json.encode(Widget(), serializer=serializer) == b'"widget"'


def test_decode_typed_lenient_coercion():
    """strict=False coerces a JSON string into the target int field."""
    item = _json.decode_typed(b'{"id": "123"}', Item, strict=False)
    assert item.id == 123


def test_decode_typed_strict_rejects_coercion():
    """strict=True rejects a string where an int is expected."""
    with pytest.raises(msgspec.ValidationError):
        _json.decode_typed(b'{"id": "123"}', Item, strict=True)

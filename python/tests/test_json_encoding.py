from __future__ import annotations

import re
from array import array
from collections import ChainMap, OrderedDict, UserDict, UserList, UserString, deque
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType, SimpleNamespace
from zoneinfo import ZoneInfo

import msgspec
import pytest
from django.utils.safestring import mark_safe

from django_bolt import BoltAPI, _json
from django_bolt.testing import TestClient


class MyDecimal(Decimal):
    pass


class MyDateTime(datetime):
    pass


class MyTime(time):
    pass


class MyInt(int):
    pass


class MyFloat(float):
    pass


class MyBytes(bytes):
    pass


class MyTimedelta(timedelta):
    pass


class MyByteArray(bytearray):
    pass


# msgspec encodes the exact type itself. Only a subclass goes to DEFAULT_TYPE_ENCODERS.
SUBCLASS_VALUES = {
    "decimal": (Decimal("1.50"), MyDecimal("1.50")),
    "decimal-integer": (Decimal("5"), MyDecimal("5")),
    "decimal-nan": (Decimal("NaN"), MyDecimal("NaN")),
    "decimal-infinity": (Decimal("-Infinity"), MyDecimal("-Infinity")),
    "datetime-utc": (datetime(2020, 1, 2, 3, 4, 5, 6, tzinfo=UTC), MyDateTime(2020, 1, 2, 3, 4, 5, 6, tzinfo=UTC)),
    "time-utc": (time(3, 4, 5, tzinfo=UTC), MyTime(3, 4, 5, tzinfo=UTC)),
    "int": (7, MyInt(7)),
    "int-big": (2**63, MyInt(2**63)),
    "float": (1.5, MyFloat(1.5)),
    "float-nan": (float("nan"), MyFloat("nan")),
    "bytes": (b"ab", MyBytes(b"ab")),
    "bytearray": (bytearray(b"ab"), MyByteArray(b"ab")),
    "timedelta": (timedelta(days=1, seconds=5, microseconds=6), MyTimedelta(days=1, seconds=5, microseconds=6)),
}
EXPECTED_JSON = {
    "decimal": '"1.50"',
    "decimal-integer": '"5"',
    "decimal-nan": '"NaN"',
    "decimal-infinity": '"-Infinity"',
    "datetime-utc": '"2020-01-02T03:04:05.000006Z"',
    "time-utc": '"03:04:05Z"',
    "int": "7",
    "int-big": "9223372036854775808",
    "float": "1.5",
    "float-nan": "null",
    "bytes": '"YWI="',
    "bytearray": '"YWI="',
    "timedelta": '"P1DT5.000006S"',
}


class MyDeque(deque):
    pass


def _numbers():
    yield 1
    yield 2


# Standard types that msgspec does not encode. The value is a function, because a generator runs one time.
STANDARD_VALUES = {
    "deque": (lambda: deque([1, 2]), "[1,2]"),
    "deque-maxlen": (lambda: deque([1, 2, 3], maxlen=2), "[2,3]"),
    "deque-subclass": (lambda: MyDeque([1, 2]), "[1,2]"),
    "deque-empty": (lambda: deque(), "[]"),
    # The items of the list go through the encoder again.
    "deque-nested": (lambda: deque([Decimal("1.50"), deque([b"ab"])]), '["1.50",["YWI="]]'),
    "generator": (_numbers, "[1,2]"),
    "fraction": (lambda: Fraction(-1, 3), '"-1/3"'),
    "fraction-integer": (lambda: Fraction(4, 2), '"2"'),
    "pattern": (lambda: re.compile("a+"), '"a+"'),
    # A mapping is a JSON object. A sequence or an iterator is a JSON array.
    "chainmap": (lambda: ChainMap({"a": 1}, {"a": 2, "b": 2}), '{"a":1,"b":2}'),
    "mappingproxy": (lambda: MappingProxyType({"a": Decimal("1.50")}), '{"a":"1.50"}'),
    "userdict": (lambda: UserDict({"a": 1}), '{"a":1}'),
    "range": (lambda: range(1, 4), "[1,2,3]"),
    "array": (lambda: array("i", [1, 2]), "[1,2]"),
    "array-float": (lambda: array("d", [1.5]), "[1.5]"),
    "dict-keys": (lambda: {"a": 1, "b": 2}.keys(), '["a","b"]'),
    "dict-values": (lambda: {"a": 1, "b": 2}.values(), "[1,2]"),
    "dict-items": (lambda: {"a": 1}.items(), '[["a",1]]'),
    "ordered-dict-keys": (lambda: OrderedDict(a=1).keys(), '["a"]'),
    "userlist": (lambda: UserList([1, deque([2])]), "[1,[2]]"),
    "map": (lambda: map(str, [1, 2]), '["1","2"]'),
    "zip": (lambda: zip([1, 2], "ab", strict=True), '[[1,"a"],[2,"b"]]'),
    "userstring": (lambda: UserString("hi"), '"hi"'),
}

# Types with no obvious JSON form. Django's DjangoJSONEncoder also refuses them.
REFUSED_VALUES = {
    "zoneinfo": lambda: ZoneInfo("UTC"),
    "timezone": lambda: timezone(timedelta(hours=1)),
    "namespace": lambda: SimpleNamespace(a=1),
    "complex": lambda: complex(1, 2),
}


@pytest.mark.parametrize("name", list(STANDARD_VALUES))
def test_a_standard_container_or_scalar_in_a_response_encodes(name: str) -> None:
    api = BoltAPI()
    make_value, expected = STANDARD_VALUES[name]

    @api.get("/value")
    async def value():
        return {"v": make_value()}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/value")
        assert response.status_code == 200
        assert response.text == '{"v":' + expected + "}"


@pytest.mark.parametrize("name", list(REFUSED_VALUES))
def test_a_value_with_no_json_form_in_a_response_is_a_server_error(name: str) -> None:
    api = BoltAPI()

    @api.get("/value")
    async def value():
        return {"v": REFUSED_VALUES[name]()}

    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.get("/value").status_code == 500


def test_encode_django_safe_string() -> None:
    value = mark_safe("<strong>Hello</strong>")

    encoded = _json.encode({"html": value})

    assert msgspec.json.decode(encoded) == {"html": "<strong>Hello</strong>"}
    assert type(msgspec.json.decode(encoded)["html"]) is str


@pytest.mark.parametrize("name", list(SUBCLASS_VALUES))
def test_a_subclass_value_encodes_as_its_base_type(name: str) -> None:
    api = BoltAPI()

    @api.get("/base/{name}")
    async def base(name: str):
        return {"v": SUBCLASS_VALUES[name][0]}

    @api.get("/subclass/{name}")
    async def subclass(name: str):
        return {"v": SUBCLASS_VALUES[name][1]}

    with TestClient(api) as client:
        expected = '{"v":' + EXPECTED_JSON[name] + "}"
        assert client.get(f"/base/{name}").text == expected
        response = client.get(f"/subclass/{name}")
        assert response.status_code == 200
        assert response.text == expected

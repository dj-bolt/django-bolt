from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

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
    "timedelta": '"P1DT5.000006S"',
}


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

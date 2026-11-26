"""Each request source converts the text of an int, float, date, time or datetime with one parser.

The reference is `msgspec.convert(text, T, strict=False)`, which the `list[T]`
path calls. A path, query, header, cookie and form value must give the value
that msgspec gives, or a 422 when msgspec refuses the text.
"""

from __future__ import annotations

import datetime
from typing import Annotated
from urllib.parse import quote

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.param_functions import Cookie, Form, Header
from django_bolt.testing import TestClient

TYPES = {"int": int, "float": float, "date": datetime.date, "time": datetime.time, "datetime": datetime.datetime}

# Forms of the audit findings F1 to F5. A header, a cookie and a path segment
# cannot hold each character, so the texts have no space, comma or semicolon.
SAMPLES = [
    ("datetime", "2024-01-15T10:30:00+05:30"),
    ("datetime", "2024-01-15T10:30:00-00:00"),
    ("datetime", "2024-01-15T10:30:00.123+0530"),
    ("datetime", "2024-01-15t10:30:00.1234567"),
    ("datetime", "1700000000.5"),
    ("datetime", "2024-10-31T9:34:5Z"),
    ("datetime", "2024-01-15"),
    ("datetime", "2024-01-15T10:30"),
    ("date", "2024-01-15"),
    ("date", "24-01-15"),
    ("date", "+2024-01-15"),
    ("time", "10:30:00+05:00"),
    ("time", "10:30:00Z"),
    ("time", "1:2:3"),
    ("time", "10:30"),
    ("int", "9223372036854775808"),
    ("int", "-9223372036854775809"),
    ("int", "5.0"),
    ("int", "+5"),
    ("int", "007"),
    ("float", "1.5e3"),
    ("float", "+1.5"),
    ("float", "1e999"),
]


def describe(value):
    """Give the JSON form of a converted value: its type, its fields and its UTC offset."""
    if isinstance(value, datetime.datetime | datetime.time):
        offset = value.utcoffset()
        seconds = None if offset is None else offset.total_seconds()
        return [type(value).__name__, value.replace(tzinfo=None).isoformat(), seconds]
    if isinstance(value, datetime.date):
        return ["date", value.isoformat()]
    return [type(value).__name__, repr(value)]


def reference(kind: str, text: str):
    """Give msgspec's value for `text`, or None when msgspec refuses it."""
    try:
        return describe(msgspec.convert(text, TYPES[kind], strict=False))
    except msgspec.ValidationError:
        return None


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/path/int/{v}")
    async def path_int(v: int):
        return describe(v)

    @api.get("/path/float/{v}")
    async def path_float(v: float):
        return describe(v)

    @api.get("/path/date/{v}")
    async def path_date(v: datetime.date):
        return describe(v)

    @api.get("/path/time/{v}")
    async def path_time(v: datetime.time):
        return describe(v)

    @api.get("/path/datetime/{v}")
    async def path_datetime(v: datetime.datetime):
        return describe(v)

    @api.get("/query")
    async def query(
        int_: int | None = None,
        float_: float | None = None,
        date_: datetime.date | None = None,
        time_: datetime.time | None = None,
        datetime_: datetime.datetime | None = None,
    ):
        return describe(next(value for value in (int_, float_, date_, time_, datetime_) if value is not None))

    @api.get("/header")
    async def header(
        x_int: Annotated[int | None, Header()] = None,
        x_float: Annotated[float | None, Header()] = None,
        x_date: Annotated[datetime.date | None, Header()] = None,
        x_time: Annotated[datetime.time | None, Header()] = None,
        x_datetime: Annotated[datetime.datetime | None, Header()] = None,
    ):
        return describe(next(value for value in (x_int, x_float, x_date, x_time, x_datetime) if value is not None))

    @api.get("/cookie")
    async def cookie(
        int_: Annotated[int | None, Cookie()] = None,
        float_: Annotated[float | None, Cookie()] = None,
        date_: Annotated[datetime.date | None, Cookie()] = None,
        time_: Annotated[datetime.time | None, Cookie()] = None,
        datetime_: Annotated[datetime.datetime | None, Cookie()] = None,
    ):
        return describe(next(value for value in (int_, float_, date_, time_, datetime_) if value is not None))

    @api.post("/form")
    async def form(
        int_: Annotated[int | None, Form()] = None,
        float_: Annotated[float | None, Form()] = None,
        date_: Annotated[datetime.date | None, Form()] = None,
        time_: Annotated[datetime.time | None, Form()] = None,
        datetime_: Annotated[datetime.datetime | None, Form()] = None,
    ):
        return describe(next(value for value in (int_, float_, date_, time_, datetime_) if value is not None))

    with TestClient(api) as test_client:
        yield test_client


def assert_converts_as_msgspec(response, kind: str, text: str) -> None:
    expected = reference(kind, text)
    if expected is None:
        assert response.status_code == 422, (kind, text, response.text)
    else:
        assert response.status_code == 200, (kind, text, response.text)
        assert response.json() == expected, (kind, text)


@pytest.mark.parametrize(("kind", "text"), SAMPLES)
def test_a_path_param_converts_as_msgspec(client, kind, text):
    assert_converts_as_msgspec(client.get(f"/path/{kind}/{quote(text, safe='')}"), kind, text)


@pytest.mark.parametrize(("kind", "text"), SAMPLES)
def test_a_query_param_converts_as_msgspec(client, kind, text):
    assert_converts_as_msgspec(client.get("/query", params={f"{kind}_": text}), kind, text)


@pytest.mark.parametrize(("kind", "text"), SAMPLES)
def test_a_header_converts_as_msgspec(client, kind, text):
    assert_converts_as_msgspec(client.get("/header", headers={f"x-{kind}": text}), kind, text)


@pytest.mark.parametrize(("kind", "text"), SAMPLES)
def test_a_cookie_converts_as_msgspec(client, kind, text):
    assert_converts_as_msgspec(client.get("/cookie", headers={"cookie": f"{kind}_={text}"}), kind, text)


@pytest.mark.parametrize(("kind", "text"), SAMPLES)
def test_a_form_field_converts_as_msgspec(client, kind, text):
    assert_converts_as_msgspec(client.post("/form", data={f"{kind}_": text}), kind, text)

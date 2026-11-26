"""Each request source reads the text of an int, float, date, time or datetime as Python does.

The reference is Python: `int()` and `float()` for ASCII text, and the
`fromisoformat()` of `date`, `time` and `datetime` on Python 3.12, 3.13 and
3.14. A single argument and a `list[...]` argument use one Rust parser, so they
accept the same text. Audit findings F1 to F5.
"""

from __future__ import annotations

import datetime
import sys
from typing import Annotated
from urllib.parse import quote

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.param_functions import Cookie, Form, Header, Query
from django_bolt.testing import TestClient, WebSocketTestClient

UTC = datetime.UTC


def tz(hours: int = 0, minutes: int = 0, seconds: int = 0, microseconds: int = 0) -> datetime.timezone:
    delta = datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds, microseconds=microseconds)
    return datetime.timezone(delta)


def dt(*fields: int, tzinfo: datetime.tzinfo | None = None) -> datetime.datetime:
    return datetime.datetime(*fields, tzinfo=tzinfo)


def t(*fields: int, tzinfo: datetime.tzinfo | None = None) -> datetime.time:
    return datetime.time(*fields, tzinfo=tzinfo)


# Why a refused text is refused. Python refuses PYTHON texts on each version.
PYTHON = "Python refuses it"
VERSIONS = "Python versions read it differently"
NOT_ISO = "Python reads it, but it is not ISO 8601; master refused it too"
NOT_ASCII = "int() or float() reads it; master refused it too"
OFFSET_SECONDS = "Python reads it, but RFC 3339 has no seconds in an offset; master refused it too"

# The digit limit that Bolt reads at startup: that of int(), at most 4300.
# Python has no limit for 0, and Bolt then keeps 4300.
PYTHON_DIGITS = sys.get_int_max_str_digits()
DIGITS = min(4300, PYTHON_DIGITS or 4300)
TOO_MANY_DIGITS = PYTHON if 0 < PYTHON_DIGITS <= 4300 else "int() reads it, but Bolt keeps 4300 digits"

# (type, text, value or the reason of the refusal). Each form of audit findings
# F1 to F5, and each form that 9d369a5a refused although master and Python read it.
FORMS = [
    # F1: an aware datetime keeps its offset. Z, +00:00 and -00:00 give timezone.utc.
    ("datetime", "2024-01-15T10:30:00+05:30", dt(2024, 1, 15, 10, 30, tzinfo=tz(5, 30))),
    ("datetime", "2024-01-15T10:30:00.5-08:00", dt(2024, 1, 15, 10, 30, 0, 500000, tzinfo=tz(-8))),
    ("datetime", "2024-01-15T10:30:00+23:59", dt(2024, 1, 15, 10, 30, tzinfo=tz(23, 59))),
    ("datetime", "2024-01-15T10:30:00-23:59", dt(2024, 1, 15, 10, 30, tzinfo=tz(-23, -59))),
    ("datetime", "9999-12-31T23:59:59-01:00", dt(9999, 12, 31, 23, 59, 59, tzinfo=tz(-1))),
    ("datetime", "0001-01-01T00:00:00+01:00", dt(1, 1, 1, tzinfo=tz(1))),
    ("datetime", "2024-01-15T10:30:00Z", dt(2024, 1, 15, 10, 30, tzinfo=UTC)),
    ("datetime", "2024-01-15T10:30:00+00:00", dt(2024, 1, 15, 10, 30, tzinfo=UTC)),
    ("datetime", "2024-01-15T10:30:00-00:00", dt(2024, 1, 15, 10, 30, tzinfo=UTC)),
    # F2: text that is not ISO 8601. chrono read it.
    ("date", "24-01-15", PYTHON),
    ("date", "2024-1-5", PYTHON),
    ("date", "+2024-01-15", PYTHON),
    ("date", " 2024-01-15", PYTHON),
    ("date", "2024-1-15", PYTHON),
    ("datetime", "24-01-15", PYTHON),
    ("datetime", "2024-1-5", PYTHON),
    ("datetime", "+2024-01-15", PYTHON),
    ("datetime", "2024-01-1510:30:00", PYTHON),
    ("datetime", "2024-10-31T9:34:5Z", PYTHON),
    ("datetime", "2024-01-15T1:2:3", PYTHON),
    ("datetime", "\t2024-01-15T10:30:00", PYTHON),
    ("datetime", "2024-01-15T 10:30:00", PYTHON),
    ("datetime", "2024-01-15  10:30:00", PYTHON),
    ("datetime", "2024-01-15T10:30:0", PYTHON),
    ("time", "\t10:30:00", PYTHON),
    ("time", "1:2:3", PYTHON),
    ("time", "10:3", PYTHON),
    ("time", "10:30:0", PYTHON),
    # F3: an int beyond i64 is a Python int, up to the digit limit of int().
    ("int", "9223372036854775808", 9223372036854775808),
    ("int", "-9223372036854775809", -9223372036854775809),
    ("int", "1" * DIGITS, (10**DIGITS - 1) // 9),
    ("int", "1" * (DIGITS + 1), TOO_MANY_DIGITS),
    ("int", "0" * (DIGITS + 1), TOO_MANY_DIGITS),
    # F4: ISO 8601 forms that Python reads.
    ("datetime", "2024-01-15T10:30", dt(2024, 1, 15, 10, 30)),
    ("datetime", "2024-01-15T10", dt(2024, 1, 15, 10)),
    ("datetime", "2024-01-15T10:30:00+0530", dt(2024, 1, 15, 10, 30, tzinfo=tz(5, 30))),
    ("datetime", "2024-01-15T10:30:00+05", dt(2024, 1, 15, 10, 30, tzinfo=tz(5))),
    ("datetime", "20240115T103000", dt(2024, 1, 15, 10, 30)),
    ("datetime", "20240115", dt(2024, 1, 15)),
    ("datetime", "2024-W03-1T10:30:00", dt(2024, 1, 15, 10, 30)),
    ("datetime", "2024W031T103000", dt(2024, 1, 15, 10, 30)),
    ("datetime", "2024-01-15T10:30:00,5", dt(2024, 1, 15, 10, 30, 0, 500000)),
    ("datetime", "2024-01-15t10:30:00", dt(2024, 1, 15, 10, 30)),
    ("date", "20240115", datetime.date(2024, 1, 15)),
    ("date", "2024-W03-1", datetime.date(2024, 1, 15)),
    ("date", "2024W03", datetime.date(2024, 1, 15)),
    ("time", "10", t(10)),
    ("time", "1030", t(10, 30)),
    ("time", "T10:30:00", t(10, 30)),
    ("time", "10:30:00,5", t(10, 30, 0, 500000)),
    ("datetime", "2024-01-15T10:30:00z", PYTHON),
    ("datetime", "2024-015", PYTHON),
    # F5: a time with an offset.
    ("time", "10:30:00+05:00", t(10, 30, tzinfo=tz(5))),
    ("time", "10:30:00.123+0530", t(10, 30, 0, 123000, tzinfo=tz(5, 30))),
    ("time", "10:30:00Z", t(10, 30, tzinfo=UTC)),
    ("time", "10:30:00-00:00", t(10, 30, tzinfo=UTC)),
    ("time", "10:30:00z", PYTHON),
    # Master and Python read these texts with the same value, so they stay valid.
    ("int", "007", 7),
    ("int", "-007", -7),
    ("int", "+5", 5),
    ("float", ".5", 0.5),
    ("float", "5.", 5.0),
    ("float", "+1.5", 1.5),
    ("float", "+inf", float("inf")),
    ("float", "1e999", float("inf")),
    ("float", "-0", -0.0),
    ("datetime", "2024-01-15", dt(2024, 1, 15)),
    ("datetime", "2024-01-15 10:30:00", dt(2024, 1, 15, 10, 30)),
    ("datetime", "2024-01-15\t10:30:00", dt(2024, 1, 15, 10, 30)),
    ("datetime", "2024-01-15T10:30:00.9999995", dt(2024, 1, 15, 10, 30, 0, 999999)),
    ("time", "10:30", t(10, 30)),
    ("time", "23:59:59.9999995", t(23, 59, 59, 999999)),
    # Python refuses these texts. msgspec, and so the list path of master, read them.
    ("int", "5.0", PYTHON),
    ("int", "1e3", PYTHON),
    ("datetime", "1700000000", PYTHON),
    ("datetime", "-1.5", PYTHON),
    # Python versions read these texts differently.
    ("time", "24:00", VERSIONS),
    ("datetime", "2024-01-15T24:00:00", VERSIONS),
    ("time", "10:30.5", VERSIONS),
    ("datetime", "2024-01-15T10.5", VERSIONS),
    ("time", "10:30:00+00:00:00.5", VERSIONS),
    # Python reads these texts, but they are not ISO 8601. Master refused them too.
    ("datetime", "2024-01-15x10:30:00", NOT_ISO),
    ("datetime", "2024-01-15T10:30:00 +05:30", NOT_ISO),
    ("datetime", "2024-01-15T10:30:00.Z", NOT_ISO),
    ("time", "10:30:00+05:60", NOT_ISO),
    ("time", "10:30:00\0", NOT_ISO),
    ("datetime", "2024-01-15T10:30:00\0", NOT_ISO),
    ("datetime", "2024-01-15T10:30:00+05:30:15.5", OFFSET_SECONDS),
    ("datetime", "2024-01-15T10:30:00+05:30:15", OFFSET_SECONDS),
    ("datetime", "2024-01-15T10:30:00+05:30:00", OFFSET_SECONDS),
    ("datetime", "2024-01-15T10:30:00-00:00:00", OFFSET_SECONDS),
    ("time", "10:30:00+053015", OFFSET_SECONDS),
    ("time", "10:30:00-23:59:59.999999", OFFSET_SECONDS),
    ("date", "20240115xx", NOT_ISO),
    ("date", "0002121227", NOT_ISO),
    ("datetime", "2024-01-15\x1c10:30", NOT_ISO),
    ("datetime", "2024-01-15\x1f10:30", NOT_ISO),
    ("int", "1_000", NOT_ASCII),
    ("int", " 5", NOT_ASCII),
    ("int", "٣", NOT_ASCII),
    ("float", "1_000.5", NOT_ASCII),
    ("float", "1.5 ", NOT_ASCII),
]
PYTHON_READERS = {
    "int": int,
    "float": float,
    "date": datetime.date.fromisoformat,
    "time": datetime.time.fromisoformat,
    "datetime": datetime.datetime.fromisoformat,
}
TYPES = {"int": int, "float": float, "date": datetime.date, "time": datetime.time, "datetime": datetime.datetime}


def describe(value):
    """Give the JSON form of a converted value: its type, its fields and its UTC offset.

    An int is in hex: str() and repr() refuse more digits than the digit limit of int().
    """
    if isinstance(value, datetime.datetime | datetime.time):
        offset = value.utcoffset()
        seconds = None if offset is None else offset.total_seconds()
        return [type(value).__name__, value.replace(tzinfo=None).isoformat(), seconds, value.tzinfo is UTC]
    if isinstance(value, datetime.date):
        return ["date", value.isoformat()]
    if type(value) is int:
        return ["int", hex(value)]
    return [type(value).__name__, repr(value)]


api = BoltAPI()


@api.get("/scalar/int")
async def scalar_int(v: int):
    return describe(v)


@api.get("/scalar/float")
async def scalar_float(v: float):
    return describe(v)


@api.get("/scalar/date")
async def scalar_date(v: datetime.date):
    return describe(v)


@api.get("/scalar/time")
async def scalar_time(v: datetime.time):
    return describe(v)


@api.get("/scalar/datetime")
async def scalar_datetime(v: datetime.datetime):
    return describe(v)


@api.get("/list/int")
async def list_int(v: Annotated[list[int], Query()]):
    return describe(v[0])


@api.get("/list/float")
async def list_float(v: Annotated[list[float], Query()]):
    return describe(v[0])


@api.get("/list/date")
async def list_date(v: Annotated[list[datetime.date], Query()]):
    return describe(v[0])


@api.get("/list/time")
async def list_time(v: Annotated[list[datetime.time], Query()]):
    return describe(v[0])


@api.get("/list/datetime")
async def list_datetime(v: Annotated[list[datetime.datetime], Query()]):
    return describe(v[0])


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


def first(*values):
    return next(value for value in values if value is not None)


@api.get("/query")
async def query(
    int_: int | None = None,
    float_: float | None = None,
    date_: datetime.date | None = None,
    time_: datetime.time | None = None,
    datetime_: datetime.datetime | None = None,
):
    return describe(first(int_, float_, date_, time_, datetime_))


@api.get("/header")
async def header(
    x_int: Annotated[int | None, Header()] = None,
    x_float: Annotated[float | None, Header()] = None,
    x_date: Annotated[datetime.date | None, Header()] = None,
    x_time: Annotated[datetime.time | None, Header()] = None,
    x_datetime: Annotated[datetime.datetime | None, Header()] = None,
):
    return describe(first(x_int, x_float, x_date, x_time, x_datetime))


@api.get("/header-list")
async def header_list(
    x_int: Annotated[list[int] | None, Header()] = None,
    x_float: Annotated[list[float] | None, Header()] = None,
    x_date: Annotated[list[datetime.date] | None, Header()] = None,
    x_time: Annotated[list[datetime.time] | None, Header()] = None,
    x_datetime: Annotated[list[datetime.datetime] | None, Header()] = None,
):
    return describe(first(x_int, x_float, x_date, x_time, x_datetime)[0])


@api.get("/cookie")
async def cookie(
    int_: Annotated[int | None, Cookie()] = None,
    float_: Annotated[float | None, Cookie()] = None,
    date_: Annotated[datetime.date | None, Cookie()] = None,
    time_: Annotated[datetime.time | None, Cookie()] = None,
    datetime_: Annotated[datetime.datetime | None, Cookie()] = None,
):
    return describe(first(int_, float_, date_, time_, datetime_))


@api.post("/form")
async def form(
    int_: Annotated[int | None, Form()] = None,
    float_: Annotated[float | None, Form()] = None,
    date_: Annotated[datetime.date | None, Form()] = None,
    time_: Annotated[datetime.time | None, Form()] = None,
    datetime_: Annotated[datetime.datetime | None, Form()] = None,
):
    return describe(first(int_, float_, date_, time_, datetime_))


@api.post("/form-list")
async def form_list(
    int_: Annotated[list[int] | None, Form()] = None,
    float_: Annotated[list[float] | None, Form()] = None,
    date_: Annotated[list[datetime.date] | None, Form()] = None,
    time_: Annotated[list[datetime.time] | None, Form()] = None,
    datetime_: Annotated[list[datetime.datetime] | None, Form()] = None,
):
    return describe(first(int_, float_, date_, time_, datetime_)[0])


@api.websocket("/ws")
async def socket(websocket: WebSocket, int_: int | None = None, datetime_: datetime.datetime | None = None):
    await websocket.accept()
    await websocket.send_json(describe(first(int_, datetime_)))


@api.websocket("/ws/datetime/{v}")
async def socket_path(websocket: WebSocket, v: datetime.datetime):
    await websocket.accept()
    await websocket.send_json(describe(v))


@pytest.fixture(scope="module")
def client():
    with TestClient(api) as test_client:
        yield test_client


def assert_converts(response, kind: str, text: str, expected) -> None:
    if isinstance(expected, str):
        assert response.status_code == 422, (kind, text, response.text)
    else:
        assert response.status_code == 200, (kind, text, response.text)
        assert response.json() == describe(expected), (kind, text)


@pytest.mark.parametrize(("kind", "text", "expected"), FORMS)
def test_the_table_matches_python(kind, text, expected):
    """Each value of the table is the value of Python, and each text that Python refuses is refused."""
    read = PYTHON_READERS[kind]
    if expected == PYTHON:
        with pytest.raises(ValueError):
            read(text)
    elif not isinstance(expected, str):
        assert describe(read(text)) == describe(expected)


@pytest.mark.parametrize(("kind", "text", "expected"), FORMS)
def test_a_scalar_and_a_list_query_param_convert_each_form_of_the_table(client, kind, text, expected):
    assert_converts(client.get(f"/scalar/{kind}", params={"v": text}), kind, text, expected)
    assert_converts(client.get(f"/list/{kind}", params={"v": text}), kind, text, expected)


# The forms that a path segment, a header and a cookie can hold: ASCII, no whitespace,
# no comma, no semicolon and no control character.
SAMPLES = [
    (kind, text, expected)
    for kind, text, expected in FORMS
    if text.isascii() and text.isprintable() and not any(char in text for char in " ,;") and len(text) < 100
]


@pytest.mark.parametrize(("kind", "text", "expected"), SAMPLES)
def test_a_path_param_converts_as_python(client, kind, text, expected):
    assert_converts(client.get(f"/path/{kind}/{quote(text, safe='')}"), kind, text, expected)


@pytest.mark.parametrize(("kind", "text", "expected"), SAMPLES)
def test_a_query_param_converts_as_python(client, kind, text, expected):
    assert_converts(client.get("/query", params={f"{kind}_": text}), kind, text, expected)


@pytest.mark.parametrize(("kind", "text", "expected"), SAMPLES)
def test_a_header_converts_as_python(client, kind, text, expected):
    assert_converts(client.get("/header", headers={f"x-{kind}": text}), kind, text, expected)
    assert_converts(client.get("/header-list", headers={f"x-{kind}": text}), kind, text, expected)


@pytest.mark.parametrize(("kind", "text", "expected"), SAMPLES)
def test_a_cookie_converts_as_python(client, kind, text, expected):
    assert_converts(client.get("/cookie", headers={"cookie": f"{kind}_={text}"}), kind, text, expected)


@pytest.mark.parametrize(("kind", "text", "expected"), FORMS)
def test_a_form_field_converts_as_python(client, kind, text, expected):
    assert_converts(client.post("/form", data={f"{kind}_": text}), kind, text, expected)
    assert_converts(client.post("/form-list", data={f"{kind}_": text}), kind, text, expected)


WEBSOCKET_SAMPLES = [(kind, text, expected) for kind, text, expected in SAMPLES if kind in ("int", "datetime")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "text", "expected"), WEBSOCKET_SAMPLES)
async def test_a_websocket_query_param_converts_as_python(kind, text, expected):
    query = f"{kind}_={quote(text, safe='')}"
    if isinstance(expected, str):
        with pytest.raises(ValueError, match=f"Query parameter '{kind}_'"):
            async with WebSocketTestClient(api, "/ws", query_string=query):
                pass
        return
    async with WebSocketTestClient(api, "/ws", query_string=query) as websocket:
        assert await websocket.receive_json() == describe(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"), [(text, expected) for kind, text, expected in SAMPLES if kind == "datetime"]
)
async def test_a_websocket_path_param_converts_as_python(text, expected):
    path = f"/ws/datetime/{quote(text, safe='')}"
    if isinstance(expected, str):
        with pytest.raises(ValueError, match="Path parameter 'v'"):
            async with WebSocketTestClient(api, path):
                pass
        return
    async with WebSocketTestClient(api, path) as websocket:
        assert await websocket.receive_json() == describe(expected)


def test_a_list_item_error_names_the_item():
    api = BoltAPI()

    @api.get("/ids")
    async def ids(v: Annotated[list[int], Query()]):
        return v

    with TestClient(api) as client:
        response = client.get("/ids", params={"v": ["1", "5.0"]})

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "validation_error",
                "loc": ["query", "v"],
                "msg": "Invalid integer '5.0': expected a whole number such as 42 or -7 - at `$[1]`",
                "input": ["1", "5.0"],
            }
        ]
    }


@pytest.fixture
def int_max_str_digits():
    """Restore the digit limit of int(), and make the next client read it again."""
    limit = sys.get_int_max_str_digits()
    yield
    sys.set_int_max_str_digits(limit)
    # Bolt reads the limit when a client starts. A client of another test then reads the restored limit.
    with TestClient(BoltAPI()):
        pass


def test_an_int_has_at_most_the_digits_that_int_accepts_at_startup(int_max_str_digits):
    api = BoltAPI()

    @api.get("/int")
    async def read(v: int):
        return {"digits": len(str(v))}

    sys.set_int_max_str_digits(640)
    with TestClient(api) as client:
        assert client.get("/int", params={"v": "9" * 640}).json() == {"digits": 640}
        response = client.get("/int", params={"v": "9" * 641})
        assert response.status_code == 422
        assert response.json() == {
            "detail": f"Query parameter 'v': Invalid integer '{'9' * 641}': more than 640 digits"
        }
        # Bolt reads the limit once, when the client starts. A later change has no effect.
        sys.set_int_max_str_digits(0)
        assert client.get("/int", params={"v": "9" * 641}).status_code == 422

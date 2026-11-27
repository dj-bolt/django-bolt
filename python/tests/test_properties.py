"""Property tests: generated requests go through the full TestClient pipeline.

Each test states a rule for every input, not for a few examples. Hypothesis
makes the inputs. When a rule fails, Hypothesis shrinks the input to a small
example and stores it in `.hypothesis/`, so the next run tries it first.

Where Django has the same parser, the rule is "Bolt gives what Django gives".
"""

from __future__ import annotations

import datetime
import decimal
import itertools
import re
import sys
import time
import types
import unicodedata
import uuid
from http.cookies import SimpleCookie
from typing import Annotated
from urllib.parse import quote

import httpx
import jwt
import pytest
from django.http import HttpResponse, QueryDict
from django.http.cookie import parse_cookie
from django.test import override_settings
from django.urls import include, reverse
from django.urls import path as django_path
from hypothesis import event, example, given
from hypothesis import strategies as st

from django_bolt import BoltAPI
from django_bolt._urlconf import join_mount_path, urlconf_mounts
from django_bolt.auth import JWTAuthentication
from django_bolt.auth.revocation import DjangoCacheRevocation, DjangoORMRevocation
from django_bolt.param_functions import Cookie, Form, Query
from django_bolt.responses import JSON, FileResponse
from django_bolt.testing import TestClient

from . import jwt_parity

api = BoltAPI()


@api.get("/selected-cookie")
def selected_cookie(a: Annotated[str, Cookie()] = "missing"):
    return {"a": a}


@api.get("/query")
async def query_map(request):
    return dict(request.query)


@api.get("/sequence")
async def sequence(k: Annotated[list[str], Query()] = []):  # noqa: B006
    return k


@api.post("/form")
async def form(k: Annotated[list[str], Form()] = []):  # noqa: B006
    return k


@api.get("/path/{value}")
async def path_value(value: str):
    return {"value": value}


@api.get("/int")
async def int_value(v: int):
    return {"v": v}


@api.get("/float")
async def float_value(v: float):
    return {"v": repr(v)}


@api.get("/bool")
async def bool_value(v: bool):
    return {"v": v}


@api.get("/uuid")
async def uuid_value(v: uuid.UUID):
    return {"v": str(v)}


@api.get("/decimal")
async def decimal_value(v: decimal.Decimal):
    return {"v": str(v)}


@api.get("/date")
async def date_value(v: datetime.date):
    return {"v": v.isoformat()}


@api.get("/datetime")
async def datetime_value(v: datetime.datetime):
    return {"v": v.isoformat()}


@api.get("/time")
async def time_value(v: datetime.time):
    return {"v": v.isoformat()}


def _describe(value):
    """Give the JSON form of a converted value: its type, its fields and its UTC offset.

    An int is in hex: str() and repr() refuse more digits than the digit limit of int().
    """
    if isinstance(value, datetime.datetime | datetime.time):
        offset = value.utcoffset()
        seconds = None if offset is None else offset.total_seconds()
        utc = value.tzinfo is datetime.UTC
        return [type(value).__name__, value.replace(tzinfo=None).isoformat(), seconds, utc]
    if isinstance(value, datetime.date):
        return ["date", value.isoformat()]
    if type(value) is int:
        return ["int", hex(value)]
    return [type(value).__name__, repr(value)]


@api.get("/scalar/int")
async def scalar_int(v: int):
    return _describe(v)


@api.get("/list/int")
async def list_int(v: Annotated[list[int], Query()]):
    return _describe(v[0])


@api.get("/scalar/float")
async def scalar_float(v: float):
    return _describe(v)


@api.get("/list/float")
async def list_float(v: Annotated[list[float], Query()]):
    return _describe(v[0])


@api.get("/scalar/date")
async def scalar_date(v: datetime.date):
    return _describe(v)


@api.get("/list/date")
async def list_date(v: Annotated[list[datetime.date], Query()]):
    return _describe(v[0])


@api.get("/scalar/time")
async def scalar_time(v: datetime.time):
    return _describe(v)


@api.get("/list/time")
async def list_time(v: Annotated[list[datetime.time], Query()]):
    return _describe(v[0])


@api.get("/scalar/datetime")
async def scalar_datetime(v: datetime.datetime):
    return _describe(v)


@api.get("/list/datetime")
async def list_datetime(v: Annotated[list[datetime.datetime], Query()]):
    return _describe(v[0])


@api.get("/cookies")
async def cookie_map(request):
    return dict(request.cookies)


@api.get("/set-cookie")
async def set_cookie(v: str):
    return JSON({}).set_cookie("k", v)


TYPED_PATHS = ["/int", "/float", "/bool", "/uuid", "/decimal", "/date", "/datetime", "/time"]


@pytest.fixture(scope="module")
def client():
    with TestClient(api) as test_client:
        yield test_client


# Pieces of a raw query string: plain characters, the separators, valid and
# broken escapes, and escapes of UTF-8 bytes, valid and not.
QUERY_PIECES = [
    *"aZ09-._~!$'()*,;:@/?",
    "k",
    "%",
    "+",
    "&",
    "=",
    "%2",
    "%G1",
    "%20",
    "%2B",
    "%26",
    "%3D",
    "%25",
    "%C3%A9",
    "%E2%82%AC",
    "%F0%9F%98%80",
    "%FF",
    "%C3",
    "%E2%82",
    "é",
]
raw_queries = st.lists(st.sampled_from(QUERY_PIECES), max_size=40).map("".join)


def _sent_query(raw: str) -> str:
    """Give the query string that the client sends for `raw`.

    httpx escapes some characters, for example `é`. The server gets that
    string, so Django must parse that string too.
    """
    return httpx.URL(f"http://testserver/?{raw}").query.decode("ascii")


@given(raw=raw_queries)
def test_query_values_match_django(client, raw):
    sent = _sent_query(raw)
    response = client.get(f"/query?{sent}")
    assert response.status_code == 200
    assert response.json() == dict(QueryDict(sent).items())


@given(raw=raw_queries)
def test_a_sequence_query_param_matches_django_getlist(client, raw):
    sent = _sent_query(raw)
    response = client.get(f"/sequence?{sent}")
    assert response.status_code == 200
    assert response.json() == QueryDict(sent).getlist("k")


@given(raw=raw_queries)
def test_a_urlencoded_form_matches_django_getlist(client, raw):
    response = client.post(
        "/form",
        content=raw.encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert response.json() == QueryDict(raw).getlist("k")


# httpx removes the dot segments "." and "..", as a browser does.
@given(value=st.text(min_size=1).filter(lambda text: text not in {".", ".."}))
def test_a_path_param_round_trips(client, value):
    response = client.get(f"/path/{quote(value, safe='')}")
    assert response.status_code == 200
    assert response.json() == {"value": value}


@given(value=st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_an_int_query_param_round_trips(client, value):
    response = client.get("/int", params={"v": str(value)})
    assert response.json() == {"v": value}


@given(value=st.floats(allow_nan=False))
def test_a_float_query_param_round_trips(client, value):
    response = client.get("/float", params={"v": repr(value)})
    assert response.json() == {"v": repr(value)}


@given(value=st.booleans(), word=st.integers(min_value=0, max_value=3), upper=st.lists(st.booleans(), min_size=5))
def test_a_bool_query_param_accepts_each_word_in_any_case(client, value, word, upper):
    text = (["true", "1", "yes", "on"] if value else ["false", "0", "no", "off"])[word]
    text = "".join(char.upper() if flag else char for char, flag in zip(text, upper, strict=False))
    response = client.get("/bool", params={"v": text})
    assert response.json() == {"v": value}


@given(value=st.uuids(), form=st.sampled_from([str, lambda u: u.hex, lambda u: u.urn, lambda u: f"{{{u}}}"]))
def test_a_uuid_query_param_round_trips(client, value, form):
    response = client.get("/uuid", params={"v": form(value)})
    assert response.json() == {"v": str(value)}


@given(value=st.decimals(allow_nan=False, allow_infinity=False))
def test_a_decimal_query_param_round_trips(client, value):
    response = client.get("/decimal", params={"v": str(value)})
    assert response.json() == {"v": str(value)}


@given(value=st.dates())
def test_a_date_query_param_round_trips(client, value):
    response = client.get("/date", params={"v": value.isoformat()})
    assert response.json() == {"v": value.isoformat()}


@given(value=st.datetimes(), separator=st.sampled_from(["T", " "]))
def test_a_naive_datetime_query_param_round_trips(client, value, separator):
    response = client.get("/datetime", params={"v": value.isoformat(sep=separator)})
    assert response.json() == {"v": value.isoformat()}


# RFC 3339 offsets have whole minutes.
utc_offsets = st.integers(min_value=-(24 * 60 - 1), max_value=24 * 60 - 1).map(
    lambda minutes: datetime.timezone(datetime.timedelta(minutes=minutes))
)


@given(value=st.datetimes(), offset=utc_offsets)
def test_an_aware_datetime_query_param_keeps_its_offset(client, value, offset):
    aware = value.replace(tzinfo=offset)
    response = client.get("/datetime", params={"v": aware.isoformat()})
    returned = datetime.datetime.fromisoformat(response.json()["v"])
    assert returned.replace(tzinfo=None) == value
    assert returned.utcoffset() == aware.utcoffset()


# Texts for the parameter types that Rust converts: the forms that Python reads,
# fields out of range, and mutations of each one.
_MUTATION_CHARS = [
    " ",
    "\t",
    "\0",
    "+",
    "-",
    "0",
    "9",
    ":",
    ".",
    ",",
    "e",
    "T",
    "t",
    "x",
    "Z",
    "z",
    "W",
    "_",
    "é",
    "٣",
]


def _digits(min_size, max_size):
    return st.text("0123456789", min_size=min_size, max_size=max_size)


@st.composite
def _mutated(draw, texts):
    """Insert, delete or replace up to two characters of a text."""
    text = draw(texts)
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        at = draw(st.integers(min_value=0, max_value=len(text)))
        char = draw(st.sampled_from(_MUTATION_CHARS))
        operation = draw(st.sampled_from(["insert", "delete", "replace"]))
        if operation == "insert":
            text = text[:at] + char + text[at:]
        elif operation == "delete":
            text = text[:at] + text[at + 1 :]
        else:
            text = text[:at] + char + text[at + 1 :]
    return text


# Mostly in range. Hour 24 and minute or second 60 also occur.
_two_digit_fields = st.one_of(st.integers(min_value=0, max_value=59), st.sampled_from([23, 24, 60, 99]))


@st.composite
def _hms_texts(draw):
    """HH, HH:MM, HH:MM:SS, HHMM or HHMMSS, with a fraction after SS, or after HH or MM."""
    fields = [draw(_two_digit_fields) for _ in range(draw(st.integers(min_value=1, max_value=3)))]
    separator = draw(st.sampled_from([":", ""]))
    text = separator.join(f"{field:02}" for field in fields)
    if draw(st.booleans()):
        text += draw(st.sampled_from(".,")) + draw(_digits(0, 9))
    return text


@st.composite
def _clock_texts(draw):
    zone = draw(st.sampled_from(["", "", "Z", "+", "-"]))
    if zone in "+-":
        zone += draw(_hms_texts())
    return draw(_hms_texts()) + zone


@st.composite
def _date_texts(draw):
    day = draw(st.dates())
    year, week, weekday = day.isocalendar()
    return draw(
        st.sampled_from(
            [
                day.isoformat(),
                f"{day.year:04}{day.month:02}{day.day:02}",
                f"{year:04}-W{week:02}-{weekday}",
                f"{year:04}W{week:02}{weekday}",
                f"{year:04}-W{week:02}",
                f"{year:04}W{week:02}",
                f"{draw(_digits(4, 4))}-{draw(_digits(2, 2))}-{draw(_digits(2, 2))}",
                f"{draw(_digits(4, 4))}-W{draw(_digits(2, 2))}-{draw(_digits(1, 1))}",
            ]
        )
    )


@st.composite
def _datetime_texts(draw):
    date = draw(_date_texts())
    if draw(st.integers(min_value=0, max_value=4)) == 0:
        return date
    return date + draw(st.sampled_from(["T", "t", " ", "x", "\t", "\u3000", "\x1f", "  "])) + draw(_clock_texts())


_number_texts = st.one_of(
    st.integers().map(str),
    st.integers(min_value=-(10**60), max_value=10**60).map(str),
    st.integers(min_value=4290, max_value=4310).map(lambda length: "1" * length),
    st.integers(min_value=-(2**54), max_value=2**54).map(lambda value: f"{value}.0"),
    st.floats().map(repr),
    st.from_regex(r"[+-]?\d{0,5}(\.\d{0,5})?([eE][+-]?\d{1,3})?", fullmatch=True),
    st.sampled_from(["inf", "-Infinity", "NaN", "+nan", "+5", "007", "1_000", ".5", "5.", "0x10", "1e999"]),
)
PARAM_TEXTS = st.one_of(
    st.tuples(st.just("int"), _mutated(_number_texts)),
    st.tuples(st.just("float"), _mutated(_number_texts)),
    st.tuples(st.just("date"), _mutated(_date_texts())),
    st.tuples(st.just("time"), _mutated(st.builds("{}{}".format, st.sampled_from(["", "T"]), _clock_texts()))),
    st.tuples(st.just("datetime"), _mutated(_datetime_texts())),
)

# The ISO 8601 forms that Bolt reads (see docs/src/topics/requests.md).
_ISO_DATE = r"\d{4}(?:-\d{2}-\d{2}|\d{4}|-?W\d{2}|-W\d{2}-\d|W\d{3})"
_HMS = r"\d{2}(?::\d{2}(?::\d{2}(?:[.,]\d+)?)?|\d{2}(?:\d{2}(?:[.,]\d+)?)?)?"
# RFC 3339 has no seconds in an offset, so Bolt refuses them, although Python reads them.
_CLOCK = rf"(?P<clock>{_HMS})(?:Z|[+-](?P<zone>\d{{2}}(?::?\d{{2}})?))?"
ISO_FORMS = {
    "date": re.compile(_ISO_DATE, re.ASCII),
    "time": re.compile(rf"T?{_CLOCK}", re.ASCII),
    # The separator is T, t or one whitespace character of Rust's char::is_whitespace().
    "datetime": re.compile(
        rf"{_ISO_DATE}(?:[Tt\t\n\x0b\x0c\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]{_CLOCK})?",
        re.ASCII,
    ),
}
PARAM_TYPES = {"date": datetime.date, "time": datetime.time, "datetime": datetime.datetime}


def _python_reads_it_on_each_version(match: re.Match) -> bool:
    """False for a text of the ISO forms that Bolt refuses although a Python version reads it."""
    groups = match.groupdict()
    if (groups.get("clock") or "").startswith("24"):
        # Only Python 3.14 reads hour 24, as midnight of the next day.
        return False
    zone = groups.get("zone")
    # Each version reads +05:60 as six hours. It is not ISO 8601.
    return zone is None or int(zone.replace(":", "")[2:] or 0) <= 59


# The digit limit of an int that Bolt reads at startup: that of int(), at most 4300.
_INT_DIGITS = min(4300, sys.get_int_max_str_digits() or 4300)


def _python_value(kind: str, text: str):
    """Give the value that Python reads from `text`, or None when Bolt must refuse the text.

    Bolt reads a number as `int()` or `float()` reads it, with no whitespace,
    `_` or digits that are not ASCII. It reads an ISO 8601 form of the grammar
    as `fromisoformat()` reads it on each supported Python version.
    """
    try:
        if kind == "int":
            readable = re.fullmatch(r"[+-]?[0-9]+", text, re.ASCII) and len(text.lstrip("+-")) <= _INT_DIGITS
            return int(text) if readable else None
        if kind == "float":
            readable = text.isascii() and "_" not in text and not any(char.isspace() for char in text)
            return float(text) if readable else None
        match = ISO_FORMS[kind].fullmatch(text)
        if match is None or not _python_reads_it_on_each_version(match):
            return None
        return PARAM_TYPES[kind].fromisoformat(text)
    except ValueError:
        return None


@example(case=("datetime", "2024-01-15T10:30"))
@example(case=("datetime", "2024-01-15T10:30:00+05:30"))
@example(case=("datetime", "2024-01-15"))
@example(case=("datetime", "20240115"))
@example(case=("datetime", "2024-01-15x10:30:00"))
@example(case=("datetime", "2024-01-15T10:30:00z"))
@example(case=("datetime", "1700000000"))
@example(case=("datetime", "24-01-15"))
@example(case=("datetime", "-16964e15"))
@example(case=("time", "10:30"))
@example(case=("time", "10:30:00+05:00"))
@example(case=("time", "10:30:00 +05:00"))
@example(case=("time", "24:00"))
@example(case=("time", "10:30.5"))
@example(case=("time", "10:30:00+00:00:00.5"))
@example(case=("time", "10:30:00+05:60"))
@example(case=("time", "10:30:00+05:30:15"))
@example(case=("datetime", "2024-01-15T10:30:00+05:30:00"))
@example(case=("date", "20240115xx"))
@example(case=("int", "9223372036854775808"))
@example(case=("int", "007"))
@example(case=("int", "+5"))
@example(case=("int", "5.0"))
@example(case=("int", "-16964e15"))
@example(case=("float", ".5"))
@example(case=("float", "-0"))
@example(case=("float", "1e999"))
@given(case=PARAM_TEXTS)
def test_a_scalar_and_a_list_param_convert_as_python(client, case):
    """A scalar and a list parameter of one type accept the texts that Python reads, with the same value.

    The reference is `int()`, `float()` and `fromisoformat()`. See `_python_value`
    for the texts that Python reads and Bolt refuses.
    """
    kind, text = case
    value = _python_value(kind, text)
    for path in (f"/scalar/{kind}", f"/list/{kind}"):
        response = client.get(path, params={"v": text})
        if value is None:
            assert response.status_code == 422, (path, text, response.text)
        else:
            assert response.status_code == 200, (path, text, response.text)
            assert response.json() == _describe(value), (path, text)


@given(value=st.times())
def test_a_time_query_param_round_trips(client, value):
    response = client.get("/time", params={"v": value.isoformat()})
    assert response.json() == {"v": value.isoformat()}


# Strings near the formats that the coercion accepts: each part can be out of
# range, short, or missing.
near_temporal = st.from_regex(
    r"\d{1,5}-\d{1,3}-\d{1,3}([T ]\d{1,3}:\d{1,3}(:\d{1,3}(\.\d{1,10})?)?(Z|[+-]\d{2}:\d{2})?)?"
    r"|\d{1,3}:\d{1,3}(:\d{1,3}(\.\d{1,10})?)?",
    fullmatch=True,
)
near_numbers = st.from_regex(r"[+-]?\d{0,25}(\.\d{0,25})?([eE][+-]?\d{0,25})?", fullmatch=True)
any_values = st.one_of(st.text(), near_temporal, near_numbers)


@given(path=st.sampled_from(TYPED_PATHS), value=any_values)
def test_a_typed_query_param_answers_200_or_422(client, path, value):
    response = client.get(path, params={"v": value})
    assert response.status_code in {200, 422}, response.text


# Each field runs one past its range, so a leap second (60) and an hour of 24 occur.
hours = st.integers(min_value=0, max_value=24)
minutes = st.integers(min_value=0, max_value=60)
seconds = st.integers(min_value=0, max_value=61)


@example(hour=23, minute=59, second=60)
@given(hour=hours, minute=minutes, second=seconds)
def test_an_accepted_time_keeps_its_fields(client, hour, minute, second):
    """A time is accepted as it is, or refused. It is never changed."""
    response = client.get("/time", params={"v": f"{hour:02}:{minute:02}:{second:02}"})
    if response.status_code == 422:
        return
    returned = datetime.time.fromisoformat(response.json()["v"])
    assert (returned.hour, returned.minute, returned.second) == (hour, minute, second)


@example(hour=23, minute=59, second=60, zone="Z")
@given(hour=hours, minute=minutes, second=seconds, zone=st.sampled_from(["", "Z", "+00:00"]))
def test_an_accepted_datetime_keeps_its_fields(client, hour, minute, second, zone):
    """A datetime is accepted as it is, or refused. It is never changed."""
    response = client.get("/datetime", params={"v": f"2016-12-31T{hour:02}:{minute:02}:{second:02}{zone}"})
    if response.status_code == 422:
        return
    returned = datetime.datetime.fromisoformat(response.json()["v"])
    assert (returned.hour, returned.minute, returned.second) == (hour, minute, second)


# Pieces of a raw Cookie header: separators, spaces, quotes, and escapes.
COOKIE_PIECES = [*"aZ09-_.!", "k", "=", ";", " ", "\t", '"', "\\", ",", "\\054", '\\"', "\\x"]


cookie_headers = st.lists(st.sampled_from(COOKIE_PIECES), max_size=30).map("".join)


@given(raw=cookie_headers)
def test_cookies_match_django(client, raw):
    response = client.get("/cookies", headers={"cookie": raw})
    assert response.json() == parse_cookie(raw)


@given(lines=st.lists(cookie_headers, min_size=2, max_size=4))
def test_repeated_cookie_lines_match_django(client, lines):
    """Django joins the `Cookie` lines of a request with "; ", so each line counts."""
    response = client.get("/cookies", headers=[("cookie", line) for line in lines])
    # Django 5.2 joins the lines with ",". Thus build the joined header as Django 6 does.
    assert response.json() == parse_cookie("; ".join(line.rstrip("; ") for line in lines))


# Django escapes each Latin-1 character in a value, and refuses a control character.
CONTROL_CHARACTERS = [*map(chr, range(32)), "\x7f"]
latin1_text = st.text(alphabet=st.characters(max_codepoint=255, exclude_characters=CONTROL_CHARACTERS))


def _browser_cookie(set_cookie_header: str) -> tuple[str, str, list[str]]:
    """Split a Set-Cookie header as a browser does (RFC 6265, section 5.2)."""
    pair, *attributes = set_cookie_header.split(";")
    name, value = pair.split("=", 1)
    return name.strip(), value.strip(), [attribute.strip() for attribute in attributes]


@given(value=latin1_text)
def test_a_cookie_value_is_written_as_django_writes_it(client, value):
    response = client.get("/set-cookie", params={"v": value})
    name, written, attributes = _browser_cookie(response.headers["set-cookie"])
    expected = SimpleCookie()
    expected["k"] = value
    assert (name, written) == ("k", expected["k"].coded_value)
    assert sorted(attributes) == ["Path=/", "SameSite=Lax"]


@given(value=latin1_text)
def test_a_cookie_value_reads_back_unchanged(client, value):
    response = client.get("/set-cookie", params={"v": value})
    name, written, _ = _browser_cookie(response.headers["set-cookie"])
    assert client.get("/cookies", headers={"cookie": f"{name}={written}"}).json() == {"k": value}


@given(value=latin1_text, control=st.sampled_from(CONTROL_CHARACTERS), at=st.integers(min_value=0))
def test_a_cookie_value_with_a_control_character_is_not_set(client, value, control, at):
    at %= len(value) + 1
    response = client.get("/set-cookie", params={"v": value[:at] + control + value[at:]})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers


@given(
    value=latin1_text, other=st.characters(min_codepoint=0x100, exclude_categories=("Cs",)), at=st.integers(min_value=0)
)
def test_a_cookie_value_above_latin1_is_not_set(client, value, other, at):
    """Django cannot send it. A browser that sends it back makes the `Cookie` header unreadable."""
    at %= len(value) + 1
    response = client.get("/set-cookie", params={"v": value[:at] + other + value[at:]})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers


@given(parts=st.lists(st.sampled_from(["a=x", "a=y", 'a="a\\054b"', "unused=z", "a=", "bare"]), max_size=20))
def test_selected_cookie_matches_django(client, parts):
    header = "; ".join(parts)
    response = client.get("/selected-cookie", headers={"cookie": header})
    assert response.status_code == 200
    assert response.json() == {"a": parse_cookie(header).get("a", "missing")}


# The parts of a generated FileResponse path. The tree of `file_client` has each name.
FILE_PATH_PARTS = [
    "public",
    "outside",
    "probe",
    "escape",
    "back",
    "dangling",
    "loop",
    "known.txt",
    "missing",
    "..",
    ".",
]


@pytest.fixture(scope="module")
def file_client(tmp_path_factory):
    """Serve paths below an allowed dir, with links out of it and back into it."""
    root = tmp_path_factory.mktemp("files")
    public = root / "public"
    outside = root / "outside"
    public.mkdir()
    outside.mkdir()
    (public / "known.txt").write_text("known")
    (public / "escape").symlink_to("../outside", target_is_directory=True)
    (public / "dangling").symlink_to("../outside/probe")
    (public / "loop").symlink_to("loop")
    (outside / "back").symlink_to("../public", target_is_directory=True)
    file_api = BoltAPI()

    @file_api.get("/file")
    def file(path: str):
        return FileResponse(f"{public}/{path}")

    with override_settings(BOLT_ALLOWED_FILE_PATHS=[str(public)]), TestClient(file_api) as test_client:
        yield test_client, outside / "probe"


@given(parts=st.lists(st.sampled_from(FILE_PATH_PARTS), min_size=1, max_size=8))
@example(parts=["..", "outside", "probe", "..", "..", "public", "known.txt"])
@example(parts=["escape", "probe", "..", "back", "known.txt"])
@example(parts=["dangling"])
@example(parts=["dangling", "..", "back"])
@example(parts=["escape", "..", "outside", "probe"])
def test_a_file_response_status_does_not_show_whether_a_file_out_of_the_allowed_dir_exists(file_client, parts):
    client, probe = file_client
    path = "/".join(parts)
    without_file = client.get("/file", params={"path": path}).status_code
    probe.write_text("secret")
    try:
        with_file = client.get("/file", params={"path": path}).status_code
    finally:
        probe.unlink()
    assert without_file == with_file


# A segment of a URLconf prefix or of a route: a static word or a parameter.
mount_words = st.from_regex(r"[a-z0-9_.~-]{1,6}", fullmatch=True)
mount_segments = st.lists(st.one_of(mount_words, st.just(None)), max_size=3)
_urlconf_names = itertools.count()


def _django_mount_view(request, **kwargs):
    return HttpResponse()


def _segments(segments: list[str | None], tag: str, django_form: str) -> tuple[str, str, dict[str, str]]:
    """Return the Django route, the Bolt route, and a value for each parameter of ``segments``."""
    django_parts, bolt_parts, values = [], [], {}
    for index, segment in enumerate(segments):
        if segment is None:
            name = f"{tag}{index}"
            django_parts.append(django_form.format(name=name))
            bolt_parts.append("{" + name + "}")
            values[name] = f"v{index}"
        else:
            django_parts.append(segment)
            bolt_parts.append(segment)
    return "/".join(django_parts), "/".join(bolt_parts), values


def _urlconf_module(urlpatterns: list) -> str:
    name = f"tests._property_urlconf_{next(_urlconf_names)}"
    module = types.ModuleType(name)
    module.urlpatterns = urlpatterns
    sys.modules[name] = module
    return name


@given(
    prefix=mount_segments,
    route=mount_segments,
    trailing_slash=st.booleans(),
    form=st.sampled_from(["<{name}>", "<str:{name}>"]),
)
@example(prefix=["missions"], route=[], trailing_slash=False, form="<{name}>")
@example(prefix=[], route=[], trailing_slash=False, form="<{name}>")
@example(prefix=[None, "missions"], route=[None], trailing_slash=True, form="<str:{name}>")
def test_a_mounted_route_path_is_the_path_that_django_reverses(prefix, route, trailing_slash, form):
    django_prefix, _bolt_prefix, prefix_values = _segments(prefix, "p", form)
    django_route, bolt_route, route_values = _segments(route, "r", "<{name}>")
    django_prefix += "/" if prefix else ""
    if route and trailing_slash:
        django_route += "/"
        bolt_route += "/"
    values = {**prefix_values, **route_values}
    api = BoltAPI()
    plain = [django_path(django_prefix, include([django_path(django_route, _django_mount_view, name="r")]))]
    names = [_urlconf_module(plain), _urlconf_module([django_path(django_prefix, api.urls)])]
    try:
        expected = reverse("r", urlconf=names[0], kwargs=values)
        (mount,) = urlconf_mounts(names[1])
        served = join_mount_path(mount.prefix, "/" + bolt_route)
        for name, value in values.items():
            served = served.replace("{" + name + "}", value)
    finally:
        for name in names:
            sys.modules.pop(name)
    assert served == expected


# A revocation store refuses a jti that a database collation can read as the key
# of a family or of a user version (#931). MySQL compares text without case or
# accents by default, and it skips some characters.

REVOCATION_SECRET = "a-secure-test-secret-that-is-long-enough"
STORE_PREFIXES = {"/cache": ("fam:", "ver:"), "/orm": ("family:", "version:")}
revocation_api = BoltAPI()


@revocation_api.get(
    "/cache", auth=[JWTAuthentication(secret=REVOCATION_SECRET, revocation_store=DjangoCacheRevocation())]
)
async def cache_store_route():
    return {"ok": True}


@revocation_api.get("/orm", auth=[JWTAuthentication(secret=REVOCATION_SECRET, revocation_store=DjangoORMRevocation())])
async def orm_store_route():
    return {"ok": True}


@pytest.fixture(scope="module")
def revocation_client(django_db_setup, django_db_blocker):
    # The ORM store reads its table for each token that it does not refuse. No test writes to it.
    with django_db_blocker.unblock(), TestClient(revocation_api) as test_client:
        yield test_client


def _bearer(jti: str) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode({"sub": "u", "iat": now, "exp": now + 3600, "jti": jti}, REVOCATION_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


# MySQL 8.0 skips each of these characters (utf8mb4_0900_ai_ci and utf8mb4_unicode_ci).
SKIPPED_CHARACTERS = ["\u00ad", "\u200b", "\u200d", "\u2060", "\ufeff", "\u034f", "\x01", "\x7f"]
# A collation of MySQL 8.0 compares each of these letters equal to the prefix letter. For example,
# utf8mb4_general_ci has ß = s and ı = i, and utf8mb4_la_0900_ai_ci has u = v.
LOOK_ALIKE_LETTERS = {"i": "ıjy", "o": "ø", "l": "ł", "s": "ß", "y": "iü", "v": "u"}
COMBINING_MARKS = [chr(n) for n in range(0x0300, 0x0363)]


@st.composite
def look_alike(draw, char: str) -> str:
    """Draw a text that a collation of MySQL 8.0 compares equal to the prefix character ``char``."""
    skipped = "".join(draw(st.lists(st.sampled_from(SKIPPED_CHARACTERS), max_size=2)))
    look_alikes = LOOK_ALIKE_LETTERS.get(char, "")
    # Each printable ASCII character has a fullwidth form at this offset.
    letter = draw(st.sampled_from([char, char.upper(), chr(ord(char) + 0xFEE0), *look_alikes]))
    if letter in look_alikes:
        # MySQL compares only the whole letter equal, with no mark and not split by NFD.
        return skipped + letter
    marks = "".join(draw(st.lists(st.sampled_from(COMBINING_MARKS), max_size=2)))
    return skipped + unicodedata.normalize(draw(st.sampled_from(["NFC", "NFD"])), letter + marks)


@st.composite
def reserved_look_alikes(draw) -> tuple[str, str]:
    """Draw a route and a jti that starts with a look-alike of a reserved prefix of its store."""
    path = draw(st.sampled_from(sorted(STORE_PREFIXES)))
    prefix = draw(st.sampled_from(STORE_PREFIXES[path]))
    return path, "".join(draw(look_alike(char)) for char in prefix) + draw(st.text(max_size=20))


@given(case=reserved_look_alikes())
@example(case=("/orm", "VERSION:u"))
@example(case=("/orm", "vérsion:u"))
@example(case=("/cache", "f\u00adam:f"))
def test_a_jti_that_a_collation_reads_as_a_reserved_key_gets_a_401(revocation_client, case):
    path, jti = case
    assert revocation_client.get(path, headers=_bearer(jti)).status_code == 401


@given(
    path=st.sampled_from(sorted(STORE_PREFIXES)),
    value=st.uuids(),
    form=st.sampled_from([str, lambda u: u.hex, lambda u: str(u).upper(), lambda u: u.hex.upper()]),
)
def test_a_uuid_or_hex_jti_is_not_refused(revocation_client, path, value, form):
    assert revocation_client.get(path, headers=_bearer(form(value))).status_code == 200


# Bolt's Rust JWT check against PyJWT (audit findings J1 to J6). Each example is a
# valid token with zero to three changes: a header parameter or a claim gets a new
# value, of its own form or of any JSON type, or goes away. Bolt and PyJWT must
# agree, except for the wanted differences that `known_difference` names. Each
# time claim is at least 29 seconds from its limit, so the few milliseconds
# between the two checks cannot change an answer.

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=6),
)
json_values = st.recursive(
    json_scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=4), inner, max_size=3)),
    max_leaves=5,
)
# Seconds from now, each at least 29 seconds from 0, -60 and 60 (the limits with a leeway of 0 or 60).
TIME_OFFSETS = [-7200, -90, -30, 30, 90, 7200]
HEADER_FORMS = {
    "alg": st.sampled_from(["HS256", "HS384", "none", "hs256"]),
    "kid": st.just("key-1"),
    "typ": st.just("JWT"),
    "crit": st.sampled_from([["b64"], ["exp"], []]),
    "b64": st.booleans(),
    "x5t": st.just("abc"),
}
CLAIM_FORMS = {
    "sub": st.sampled_from(["1", ""]),
    "jti": st.just("token-1"),
    "iss": st.sampled_from(["https://issuer.example", "https://other.example"]),
    "aud": st.sampled_from(["api", "web", ["web", "api"], [], ["api", 1]]),
    "is_staff": st.booleans(),
    "permissions": st.lists(st.sampled_from(["read", "write"]), max_size=2),
    "typ": st.sampled_from(["access", "refresh", "Bearer"]),
    "token_use": st.sampled_from(["access", "id"]),
    "role": st.just("editor"),
}
REMOVE = object()


@st.composite
def jwt_parts(draw) -> tuple[dict, dict]:
    now = int(time.time())
    fraction = st.one_of(st.just(0), st.floats(min_value=0.001, max_value=0.999))
    dates = st.builds(lambda offset, part: now + offset + part, st.sampled_from(TIME_OFFSETS), fraction)
    claim_forms = {**CLAIM_FORMS, "exp": dates, "nbf": dates, "iat": dates}
    header = {"alg": "HS256"}
    claims = {"sub": "1", "exp": now + 7200, "iss": "https://issuer.example", "aud": "api"}
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        part, forms = draw(st.sampled_from([(header, HEADER_FORMS), (claims, claim_forms)]))
        name = draw(st.sampled_from(sorted(forms)))
        value = draw(st.one_of(forms[name], forms[name], json_values, st.just(REMOVE)))
        if value is REMOVE:
            part.pop(name, None)
        else:
            part[name] = value
    return header, claims


@pytest.fixture(scope="module")
def jwt_client():
    with TestClient(jwt_parity.parity_api()) as test_client:
        yield test_client


@given(parts=jwt_parts(), backend=st.sampled_from(sorted(jwt_parity.BACKENDS)))
@example(parts=({"alg": "HS256", "kid": None}, {"exp": 2**40}), backend="plain")
@example(parts=({"alg": "HS256", "crit": ["exp"]}, {"exp": 2**40}), backend="plain")
@example(parts=({"alg": "HS256"}, {"exp": 2**40, "sub": None}), backend="plain")
@example(parts=({"alg": "HS256"}, {"exp": 2**40, "iat": 2**40}), backend="leeway")
@example(parts=({"alg": "HS256"}, {"exp": 2**40 + 0.5, "iss": 1, "is_staff": 1}), backend="plain")
def test_bolt_and_pyjwt_agree_on_a_random_token(jwt_client, parts, backend):
    header, claims = parts
    token = jwt_parity.make_token(header, claims)
    bolt = jwt_parity.bolt_accepts(jwt_client, backend, token)
    reason = jwt_parity.known_difference(header, claims)
    event("accepted" if bolt else "refused" if reason is None else "known difference")
    if reason is None:
        assert bolt == jwt_parity.pyjwt_accepts(backend, token)
    else:
        assert not bolt, reason

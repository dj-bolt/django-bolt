"""Property tests: generated requests go through the full TestClient pipeline.

Each test states a rule for every input, not for a few examples. Hypothesis
makes the inputs. When a rule fails, Hypothesis shrinks the input to a small
example and stores it in `.hypothesis/`, so the next run tries it first.

Where Django has the same parser, the rule is "Bolt gives what Django gives".
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from http.cookies import SimpleCookie
from typing import Annotated
from urllib.parse import quote

import httpx
import pytest
from django.http import QueryDict
from django.http.cookie import parse_cookie
from hypothesis import example, given
from hypothesis import strategies as st

from django_bolt import BoltAPI
from django_bolt.param_functions import Form, Query
from django_bolt.responses import JSON
from django_bolt.testing import TestClient

api = BoltAPI()


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


# RFC 3339 offsets have whole minutes. Bolt gives the handler the time in UTC,
# so the instant must be in the years that Python can show in UTC.
utc_offsets = st.integers(min_value=-(24 * 60 - 1), max_value=24 * 60 - 1).map(
    lambda minutes: datetime.timezone(datetime.timedelta(minutes=minutes))
)


@given(
    value=st.datetimes(min_value=datetime.datetime(2, 1, 1), max_value=datetime.datetime(9998, 12, 31)),
    offset=utc_offsets,
)
def test_an_aware_datetime_query_param_is_the_same_instant(client, value, offset):
    aware = value.replace(tzinfo=offset)
    response = client.get("/datetime", params={"v": aware.isoformat()})
    returned = datetime.datetime.fromisoformat(response.json()["v"])
    assert returned == aware
    assert returned.utcoffset() == datetime.timedelta(0)


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


@given(raw=st.lists(st.sampled_from(COOKIE_PIECES), max_size=30).map("".join))
def test_cookies_match_django(client, raw):
    response = client.get("/cookies", headers={"cookie": raw})
    assert response.json() == parse_cookie(raw)


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

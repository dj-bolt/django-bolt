"""Typed values that Rust can parse but Python cannot build.

Rust parses a date with year 0 and a decimal with a huge exponent. Python
`datetime.date` and `decimal.Decimal` reject them. Such a value must give a
422 that names the source, and the worker must stay up.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Annotated

import pytest

from django_bolt import BoltAPI
from django_bolt.param_functions import Cookie, Header, Query
from django_bolt.testing import TestClient

CASES = [
    ("date", "0000-01-01", "year"),
    ("date", "-0001-01-01", "year"),
    ("date", "+10000-01-01", "year"),
    ("datetime", "0000-01-01T00:00:00", "year"),
    ("datetime", "0000-01-01T00:00:00Z", "year"),
    ("datetime", "+10000-01-01T00:00:00", "year"),
    ("decimal", "1E999999999999999999999", "exponent"),
    ("decimal", "1e-99999999999999999999", "exponent"),
]


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/date/{v}")
    async def date_path(v: dt.date) -> dict:
        return {"v": str(v)}

    @api.get("/date")
    async def date_query(
        v: dt.date | None = None,
        h: Annotated[dt.date | None, Header()] = None,
        c: Annotated[dt.date | None, Cookie()] = None,
    ) -> dict:
        return {"v": str(v), "h": str(h), "c": str(c)}

    @api.get("/datetime/{v}")
    async def datetime_path(v: dt.datetime) -> dict:
        return {"v": str(v)}

    @api.get("/datetime")
    async def datetime_query(
        v: dt.datetime | None = None,
        h: Annotated[dt.datetime | None, Header()] = None,
        c: Annotated[dt.datetime | None, Cookie()] = None,
    ) -> dict:
        return {"v": str(v), "h": str(h), "c": str(c)}

    @api.get("/time")
    async def time_query(v: dt.time | None = None) -> dict:
        return {"v": str(v)}

    @api.get("/decimal/{v}")
    async def decimal_path(v: decimal.Decimal) -> dict:
        return {"v": str(v)}

    @api.get("/decimal")
    async def decimal_query(
        v: Annotated[decimal.Decimal | None, Query()] = None,
        h: Annotated[decimal.Decimal | None, Header()] = None,
        c: Annotated[decimal.Decimal | None, Cookie()] = None,
    ) -> dict:
        return {"v": str(v), "h": str(h), "c": str(c)}

    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize(("kind", "value", "reason"), CASES)
def test_path_value_python_rejects_is_422(client, kind, value, reason):
    response = client.get(f"/{kind}/{value}")
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Path parameter 'v'")
    assert reason in response.json()["detail"]


@pytest.mark.parametrize(("kind", "value", "reason"), CASES)
def test_query_value_python_rejects_is_422(client, kind, value, reason):
    response = client.get(f"/{kind}", params={"v": value})
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Query parameter 'v'")
    assert reason in response.json()["detail"]


@pytest.mark.parametrize(("kind", "value", "reason"), CASES)
def test_header_value_python_rejects_is_422(client, kind, value, reason):
    response = client.get(f"/{kind}", headers={"H": value})
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Header 'h'")
    assert reason in response.json()["detail"]


@pytest.mark.parametrize(("kind", "value", "reason"), CASES)
def test_cookie_value_python_rejects_is_422(client, kind, value, reason):
    response = client.get(f"/{kind}", cookies={"c": value})
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Cookie 'c'")
    assert reason in response.json()["detail"]


@pytest.mark.parametrize(("kind", "value"), [("time", "23:59:60"), ("datetime", "2016-12-31T23:59:60")])
def test_leap_second_is_422(client, kind, value):
    """Python has no leap second. Bolt must refuse it, not change it to 23:59:59."""
    response = client.get(f"/{kind}", params={"v": value})
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("Query parameter 'v'")
    assert "second 60" in response.json()["detail"]


def test_values_python_accepts_still_pass(client):
    assert client.get("/date/0001-01-01").json() == {"v": "0001-01-01"}
    assert client.get("/date/9999-12-31").json() == {"v": "9999-12-31"}
    assert client.get("/time", params={"v": "23:59:59.999999"}).json() == {"v": "23:59:59.999999"}
    assert client.get("/decimal/1E+2147483648").json() == {"v": "1E+2147483648"}
    assert client.get("/decimal/-0.5e-7").json() == {"v": "-5E-8"}

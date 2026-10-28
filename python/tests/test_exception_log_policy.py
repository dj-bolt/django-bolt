"""Bolt logs an unhandled exception only when its response is a 5xx.

Django's ``log_response`` does the same. A 4xx is the fault of the client,
and the access log records it at WARNING. Thus a client that sends bad
requests cannot fill the error log or the ``exception_logging_handler``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import msgspec
import pytest
from django.core.exceptions import PermissionDenied

from django_bolt import BoltAPI
from django_bolt.logging import LoggingConfig
from django_bolt.params import Query
from django_bolt.responses import JSON
from django_bolt.testing import TestClient


class Filters(msgspec.Struct):
    page: int


def _routes(api: BoltAPI) -> BoltAPI:
    # The async handlers await, so they take the async dispatch path. A
    # handler with no await takes the sync path, as a sync handler does.
    @api.get("/async/crash")
    async def async_crash():
        await asyncio.sleep(0)
        raise RuntimeError("server fault")

    @api.get("/sync/crash")
    def sync_crash():
        raise RuntimeError("server fault")

    @api.get("/async/denied")
    async def async_denied():
        await asyncio.sleep(0)
        raise PermissionDenied("no access")

    @api.get("/sync/denied")
    def sync_denied():
        raise PermissionDenied("no access")

    @api.get("/sync/invalid")
    def sync_invalid():
        raise msgspec.ValidationError("bad value")

    @api.get("/query")
    async def query(filters: Annotated[Filters, Query()]):
        return {"page": filters.page}

    @api.post("/body")
    async def body(filters: Filters):
        return {"page": filters.page}

    return api


@pytest.fixture(scope="module")
def client():
    with TestClient(_routes(BoltAPI())) as client:
        yield client


@pytest.fixture
def server_errors(caplog, monkeypatch):
    """Return a function that gives the ERROR records of ``django.server``."""
    # dictConfig() disables each existing logger that its config does not name. Thus a
    # logging test can leave django.server disabled. The tests that expect a record
    # show that the capture works.
    monkeypatch.setattr(logging.getLogger("django.server"), "disabled", False)
    caplog.set_level(logging.ERROR, logger="django.server")

    def records() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "django.server" and r.levelno >= logging.ERROR]

    return records


@pytest.mark.parametrize("path", ["/async/crash", "/sync/crash"])
def test_an_exception_with_a_5xx_response_is_logged(client, server_errors, path):
    response = client.get(path)

    assert response.status_code == 500
    assert server_errors() == [f"Exception in GET {path}: RuntimeError: server fault"]


@pytest.mark.parametrize(
    ("send", "status"),
    [
        (lambda c: c.get("/async/denied"), 403),
        (lambda c: c.get("/sync/denied"), 403),
        (lambda c: c.get("/sync/invalid"), 422),
        (lambda c: c.get("/query"), 422),
        (lambda c: c.post("/body", json={"page": "x"}), 422),
        (lambda c: c.post("/body", content=b"{", headers={"Content-Type": "application/json"}), 422),
    ],
    ids=["async-denied", "sync-denied", "sync-invalid", "query-struct", "body-struct", "malformed-body"],
)
def test_an_exception_with_a_4xx_response_is_not_logged(client, server_errors, send, status):
    response = send(client)

    assert response.status_code == status
    assert server_errors() == []


def test_the_exception_logging_handler_gets_only_5xx_exceptions():
    seen: list[str] = []

    def handler(logger, request, exc, exc_info):
        seen.append(f"{request['path']}: {type(exc).__name__}")

    api = _routes(BoltAPI(logging_config=LoggingConfig(exception_logging_handler=handler)))
    with TestClient(api) as client:
        assert client.get("/async/denied").status_code == 403
        assert client.get("/query").status_code == 422
        assert client.get("/async/crash").status_code == 500

    assert seen == ["/async/crash: RuntimeError"]


def test_the_status_of_the_formatter_response_decides(server_errors):
    def as_unavailable(error, request) -> JSON:
        return JSON({"detail": error.detail}, status_code=503)

    with TestClient(_routes(BoltAPI(error_formatter=as_unavailable))) as client:
        response = client.get("/async/denied")

    assert response.status_code == 503
    assert server_errors() == ["Exception in GET /async/denied: PermissionDenied: no access"]

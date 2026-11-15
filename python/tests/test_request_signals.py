"""Tests for the Django request signals that Bolt sends with ``BOLT_EMIT_SIGNALS``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import pytest
from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.params import Header
from django_bolt.testing import TestClient


@pytest.fixture
def started_scopes():
    """The scope of each ``request_started`` signal, in the order of the requests."""
    scopes: list[dict[str, Any]] = []

    def receiver(sender: Any, scope: dict[str, Any] | None = None, **kwargs: Any) -> None:
        scopes.append(scope)

    # Django connects close_old_connections to both signals. It checks each
    # open database connection, which a test without the database must not do.
    # The test client of Django disconnects it for the same reason.
    request_started.disconnect(close_old_connections)
    request_finished.disconnect(close_old_connections)
    request_started.connect(receiver)
    try:
        yield scopes
    finally:
        request_started.disconnect(receiver)
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)


def _plain(api: BoltAPI) -> None:
    @api.get("/route")
    async def handler():
        return {"ok": True}


def _sync(api: BoltAPI) -> None:
    @api.get("/route")
    def handler():
        return {"ok": True}


def _reads_one_header(api: BoltAPI) -> None:
    @api.get("/route")
    async def handler(x_other: Annotated[str, Header()] = ""):
        return {"ok": True}


@pytest.mark.parametrize("register", [_plain, _sync, _reads_one_header])
def test_request_started_gets_the_query_string_and_the_headers(started_scopes, register: Callable) -> None:
    """The scope has them also when the route reads no query and no header."""
    with override_settings(BOLT_EMIT_SIGNALS=True):
        api = BoltAPI()
    register(api)

    with TestClient(api) as client:
        assert client.get("/route?a=1&b=x%20y", headers={"X-Test": "one"}).status_code == 200
        assert client.get("/route").status_code == 200

    first, second = started_scopes
    assert first["type"] == "http"
    assert first["method"] == "GET"
    assert first["path"] == "/route"
    assert first["query_string"] == b"a=1&b=x%20y"
    assert first["headers"]["x-test"] == "one"
    assert second["query_string"] == b""
    assert "x-test" not in second["headers"]


def test_no_signal_without_the_setting(started_scopes) -> None:
    finished: list[Any] = []

    def receiver(sender: Any, **kwargs: Any) -> None:
        finished.append(sender)

    request_finished.connect(receiver)
    try:
        api = BoltAPI()
        _plain(api)
        with TestClient(api) as client:
            assert client.get("/route?a=1").status_code == 200
    finally:
        request_finished.disconnect(receiver)

    assert started_scopes == []
    assert finished == []

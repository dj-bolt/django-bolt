"""``BOLT_EMIT_SIGNALS``: Django's request signals around each request."""

from __future__ import annotations

import pytest
from django.core.signals import request_finished, request_started

from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException
from django_bolt.testing import TestClient


@pytest.fixture
def events():
    """The request signals that Django sends, in order, with the handler calls between them."""
    events: list[tuple] = []

    def on_started(sender, scope, **kwargs):
        events.append(("started", sender, scope["type"], scope["method"], scope["path"]))

    def on_finished(sender, **kwargs):
        events.append(("finished", sender))

    request_started.connect(on_started)
    request_finished.connect(on_finished)
    yield events
    request_started.disconnect(on_started)
    request_finished.disconnect(on_finished)


def _api(events: list[tuple]) -> BoltAPI:
    api = BoltAPI()

    @api.post("/items")
    async def create_item():
        events.append(("handler",))
        return {"ok": True}

    @api.get("/missing")
    async def missing():
        raise HTTPException(404, "No item")

    return api


@pytest.mark.django_db
def test_request_signals_wrap_each_request_when_enabled(settings, events):
    # Django's own receivers of these signals read the state of the database connections.
    settings.BOLT_EMIT_SIGNALS = True

    with TestClient(_api(events)) as client:
        assert client.post("/items").json() == {"ok": True}
        assert events == [("started", BoltAPI, "http", "POST", "/items"), ("handler",), ("finished", BoltAPI)]

        # request_finished also follows a handler that raises.
        del events[:]
        assert client.get("/missing").status_code == 404
        assert events == [("started", BoltAPI, "http", "GET", "/missing"), ("finished", BoltAPI)]


def test_no_request_signal_by_default(events):
    with TestClient(_api(events)) as client:
        assert client.post("/items").status_code == 200

    assert events == [("handler",)]

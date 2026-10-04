"""A BoltAPI lives only as long as something references it."""

from __future__ import annotations

import gc
import weakref

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


def test_an_unreferenced_api_is_freed():
    """A module-level registry kept each BoltAPI alive. The test suite builds
    about a thousand of them, so each one leaked its routes and handlers."""
    api = BoltAPI()

    @api.get("/item")
    async def item():
        return {"ok": True}

    ref = weakref.ref(api)
    del api, item
    gc.collect()

    assert ref() is None


def test_an_unclosed_test_client_releases_its_app():
    """A TestClient that nobody closes must not keep its API alive forever."""
    api = BoltAPI()

    @api.get("/item")
    async def item():
        return {"ok": True}

    client = TestClient(api)
    assert client.get("/item").json() == {"ok": True}

    ref = weakref.ref(api)
    del api, item, client
    gc.collect()

    assert ref() is None


def test_close_releases_the_app():
    api = BoltAPI()
    client = TestClient(api)
    ref = weakref.ref(api)
    client.close()
    client.api = None
    del api
    gc.collect()

    assert ref() is None

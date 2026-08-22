"""
Tests for which `@rate_limit(key=...)` values Bolt accepts.

`key="user"` and `key="api_key"` were documented but never implemented. Rust
checks the limit before it validates auth, so no identity exists at that point.
Both keys fell into the header catch-all, found no header with that name, and
resolved to the constant `"unknown"`. Every caller then shared one bucket, so a
route marked "1 rps per user" was really "1 rps in total".
"""

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient


@pytest.mark.parametrize("key", ["user", "api_key"])
def test_an_identity_key_is_rejected(key):
    """Fail at decoration instead of collapsing into one shared bucket."""
    with pytest.raises(ImproperlyConfigured, match="is not implemented"):
        rate_limit(rps=10, key=key)


@pytest.mark.parametrize("key", ["user", "api_key"])
def test_the_error_names_a_working_alternative(key):
    """A caller has to be able to act on the message."""
    with pytest.raises(ImproperlyConfigured, match="x-api-key"):
        rate_limit(rps=10, key=key)


def test_the_default_key_still_works():
    api = BoltAPI()

    @api.get("/default-key")
    @rate_limit(rps=100, burst=100)
    async def handler():
        return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/default-key").status_code == 200


def test_a_header_key_still_works():
    """The header arm is the one that always segmented correctly."""
    api = BoltAPI()

    @api.get("/header-key")
    @rate_limit(rps=100, burst=100, key="x-api-key")
    async def handler():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/header-key", headers={"x-api-key": "caller-one"})
        assert response.status_code == 200

"""
Tests for `settings.BOLT_TRUSTED_PROXIES`, the list that decides whether a
`key="ip"` rate limit may believe `X-Forwarded-For`.

Only the configuration half lives here. Everything that needs a real client
address runs against a real server, in
``python/tests/integration/test_rate_limit_client_ip_server_integration.py``:
``TestRequest`` leaves ``peer_addr`` unset, so in process every caller resolves
to the same constant and no proxy can be trusted.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient


def _api_with_limited_route():
    """Build an API with one route limited per IP."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=4, key="ip")
    async def limited():
        return {"ok": True}

    return api


@pytest.mark.parametrize("entry", ["not-an-ip", "10.0.0.0/nine", "10.0.0.0/33", "::1/129", 10, None])
def test_an_invalid_entry_fails_at_startup(entry):
    """A typo stops startup instead of quietly widening a bucket."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=["10.0.0.0/8", entry]),
        pytest.raises(ImproperlyConfigured, match="BOLT_TRUSTED_PROXIES"),
    ):
        TestClient(_api_with_limited_route())


@pytest.mark.parametrize(
    "configured",
    [
        pytest.param("10.0.0.0/8", id="string"),
        # Falsey, so a normalize-then-check order reads it as an empty list and
        # reports no proxies at all.
        pytest.param("", id="empty-string"),
        # Iterates its keys, which would look like a valid list of proxies.
        pytest.param({"10.0.0.0/8": "edge proxy"}, id="mapping"),
        pytest.param({}, id="empty-mapping"),
        # Not iterable, so the loop would raise a bare TypeError.
        pytest.param(42, id="integer"),
        pytest.param(True, id="boolean"),
    ],
)
def test_a_setting_that_is_not_a_list_fails_at_startup(configured):
    """Reject the whole setting by type, before anything iterates it."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=configured),
        pytest.raises(ImproperlyConfigured, match="must be a list"),
    ):
        TestClient(_api_with_limited_route())


@pytest.mark.parametrize("configured", [None, [], (), ["10.0.0.1"], ["10.0.0.0/8", "::1"], ("10.0.0.0/8",)])
def test_a_valid_setting_starts(configured):
    """Addresses and blocks are both accepted, in either family, list or tuple."""
    with override_settings(BOLT_TRUSTED_PROXIES=configured), TestClient(_api_with_limited_route()):
        pass


def test_an_invalid_deployment_setting_fails_even_without_a_limited_route():
    """The deployment-wide policy is validated independently of route shape."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=["not-an-ip"]),
        pytest.raises(ImproperlyConfigured, match="BOLT_TRUSTED_PROXIES"),
    ):
        api = BoltAPI()

        @api.get("/plain")
        async def plain():
            return {"ok": True}

        TestClient(api)

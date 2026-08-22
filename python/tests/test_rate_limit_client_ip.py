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


def _api_with_limited_route():
    """Build an API with one route limited per IP."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=4, key="ip")
    async def limited():
        return {"ok": True}

    return api


@pytest.mark.parametrize("entry", ["not-an-ip", "10.0.0.0/nine", "10.0.0.0/33", "::1/129"])
def test_an_invalid_entry_fails_at_registration(entry):
    """A typo stops startup instead of quietly widening a bucket."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=["10.0.0.0/8", entry]),
        pytest.raises(ImproperlyConfigured, match="BOLT_TRUSTED_PROXIES"),
    ):
        _api_with_limited_route()


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
def test_a_setting_that_is_not_a_list_fails_at_registration(configured):
    """Reject the whole setting by type, before anything iterates it."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=configured),
        pytest.raises(ImproperlyConfigured, match="must be a list"),
    ):
        _api_with_limited_route()


@pytest.mark.parametrize("configured", [None, [], (), ["10.0.0.1"], ["10.0.0.0/8", "::1"], ("10.0.0.0/8",)])
def test_a_valid_setting_registers(configured):
    """Addresses and blocks are both accepted, in either family, list or tuple."""
    with override_settings(BOLT_TRUSTED_PROXIES=configured):
        assert _api_with_limited_route() is not None


def test_a_route_without_a_rate_limit_is_unaffected():
    """Only a rate limited route reads the list, so only it should validate one."""
    with override_settings(BOLT_TRUSTED_PROXIES=["not-an-ip"]):
        api = BoltAPI()

        @api.get("/plain")
        async def plain():
            return {"ok": True}

        assert api is not None
